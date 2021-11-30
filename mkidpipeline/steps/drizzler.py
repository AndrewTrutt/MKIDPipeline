"""
Classes

    DrizzleParams   : Calculates and stores the relevant info for Drizzler
    Canvas          : Called by the "Drizzler" classes. Generates the canvas that is drizzled onto
    Drizzler        : Generates a spatially dithered 4-cube (xytw)
    DrizzledData    : Saves the drizzled data as FITS

Functions

    _increment_id    : Monkey patch for STScI drizzle class of drizzle package
    mp_worker       : Genereate a reduced, reformated photonlist
    load_data       : Consolidate all dither positions
    form            : Takes in a MKIDDitherDescription object and drizzles the dithers onto a common sky grid
"""
import os
import numpy as np
import time
import multiprocessing as mp
import matplotlib.pylab as plt
from mpl_toolkits.axes_grid1 import make_axes_locatable
from matplotlib.colors import LogNorm
import pickle
import hashlib
from glob import glob
import getpass
from mkidcore.metadata import MetadataSeries
import astropy
from astropy.io import fits
from astropy import wcs
from astropy.coordinates import EarthLocation
import astropy.units as u
from astroplan import Observer
from drizzle import drizzle as stdrizzle
import mkidcore.corelog
import mkidcore.pixelflags
import mkidcore.metadata
from mkidcore.corelog import getLogger
from mkidcore.instruments import CONEX2PIXEL
from mkidpipeline.photontable import Photontable
import mkidpipeline.config

EXCLUDE = ('pixcal.dead', 'pixcal.hot', 'pixcal.cold', 'beammap.noDacTone', 'wavecal.bad', 'wavecal.failed_convergence',
           'wavecal.no_histograms', 'wavecal.not_attempted', 'flatcal.bad') # fill with undesired flags
PROBLEM_FLAGS = tuple()  # fill with flags that will break drizzler


class StepConfig(mkidpipeline.config.BaseStepConfig):
    yaml_tag = u'!drizzler_cfg'
    REQUIRED_KEYS = (('plots', 'all', 'Which plots to generate: none|summary|all'),
                     ('pixfrac', 0.5, 'The drizzle algorithm pixel fraction'),
                     ('wcs_timestep', None, 'Seconds between different WCS (eg orientations). If None, the the '
                                            'non-blurring minimum (1 pixel at furthest dither center) will be used'),
                     ('derotate', False, 'Subtract the PA from individual frames for a derotated output. '
                                         'Overrides align_start_pa.'),
                     ('align_start_pa', False, 'Use the PA at the start of the first exposure for each frame in the '
                                               ' sequence.'),
                     ('whitelight', False, 'TODO'),
                     ('save_steps', False, 'Save intermediate fits files where possible (only some modes)'),
                     ('usecache', False, 'Cache photontable for subsequent runs'),
                     ('ncpu', 1, 'Number of CPUs to use'),
                     ('clearcache', False, 'Clear user cache on next run'))


class DrizzleParams:
    """ Calculates and stores the relevant info for Drizzler """

    def __init__(self, dither, inttime, wcs_timestep=None, pixfrac=1.0, simbad=False, startt=0):
        self.n_dithers = len(dither.obs)
        self.image_shape = dither.obs[0].beammap.shape
        self.platescale = [v.platescale.to(u.deg).value for v in dither.wcscal.values()][0]
        self.inttime = inttime
        self.pixfrac = pixfrac
        self.startt = startt
        # Get the SkyCoord type coordinates to use for center of sky grid that is drizzled onto
        self.coords = mkidcore.metadata.skycoord_from_metadata(dither.obs[0].metadata_at(), force_simbad=simbad)
        self.telescope = dither.obs[0].header.get('TELESCOP') or mkidcore.metadata.DEFAULT_CARDSET['TELESCOP'].value
        self.canvas_shape = (None, None)
        self.dith_start_times = np.array([o.start for o in dither.obs])
        self.dither_pos = np.asarray(dither.pos).T

        self.wcs_timestep = wcs_timestep or self.non_blurring_timestep()

    def non_blurring_timestep(self, allowable_pixel_smear=1, center=(0, 0)):
        """
        [1] Smart, W. M. 1962, Spherical Astronomy, (Cambridge: Cambridge University Press), p. 55

        :param allowable_pixel_smear: the resolution element threshold
        """
        # get the field rotation rate at the start of each dither
        site = astropy.coordinates.EarthLocation.of_site(self.telescope)
        apo = Observer.at_site(self.telescope)

        altaz = apo.altaz(astropy.time.Time(val=self.dith_start_times, format='unix'), self.coords)
        earthrate = 2 * np.pi / astropy.units.sday.to(astropy.units.second)

        # Smart 1962
        dith_start_rot_rates = (earthrate * np.cos(site.geodetic.lat.rad) * np.cos(altaz.az.radian) /
                                np.cos(altaz.alt.radian))

        dith_pix_offset = CONEX2PIXEL(*self.dither_pos) - CONEX2PIXEL(*center).reshape(2, 1)
        # get the minimum required timestep. One that would produce allowable_pixel_smear pixel displacement at the
        # center of furthest dither
        angle = np.arctan2(allowable_pixel_smear, np.linalg.norm(dith_pix_offset))
        max_timestep = np.abs(angle / dith_start_rot_rates).min()

        getLogger(__name__).debug(f"Maximum non-blurring time step calculated to be {max_timestep:.1f} s")

        return max_timestep


class Canvas:
    def __init__(self, dithers_data, drizzle_params, header=None, canvas_shape=(None, None), force_square_grid=True):
        """
        generates the canvas that is drizzled onto

        TODO determine appropriate value from area coverage of dataset and oversampling, even longerterm there
         the oversampling should be selected to optimize total phase coverage to extract the most resolution at a
         desired minimum S/N

        :param dithers_data:
        :param coords:
        :param canvas_shape: (int, int). RA dec samples. If either are None both are set to minimum required to
            fit all dithers
        """
        self.canvas_shape = canvas_shape
        self.dithers_data = dithers_data
        self.drizzle_params = drizzle_params
        self.shape = drizzle_params.image_shape
        self.vPlateScale = drizzle_params.platescale
        self.center = drizzle_params.coords
        self.header = header
        self.stack = None
        self.metadata = self.combine_metadata()

        if canvas_shape[0] is None or canvas_shape[1] is None:
            dith_pix_min = np.zeros((len(dithers_data), 2))
            dith_pix_max = np.zeros((len(dithers_data), 2))
            for ip, photonlist in enumerate(dithers_data):
                # find the max and min coordinate for each dither (assuming those occur at the beginning/end of
                # the dither)
                dith_pix_span = np.vstack((photonlist['obs_wcs_seq'][0].wcs.crpix,
                                           photonlist['obs_wcs_seq'][-1].wcs.crpix))
                dith_pix_min[ip] = np.min(dith_pix_span, axis=0)  # takes the min of both ra and dec
                dith_pix_max[ip] = np.max(dith_pix_span, axis=0)

            # find the min and max coordinate of all dithers
            pix_ra_span = [min(dith_pix_min[:, 0]), max(dith_pix_max[:, 0])]
            pix_dec_span = [min(dith_pix_min[:, 1]), min(dith_pix_min[:, 1])]

            # Set size of virtual grid to accommodate the limits of the offsets.
            # max_detector_dist = np.linalg.norm(self.shape)
            buffer = 100
            # if any part of the image falls off the canvas it can cause stsci.drizzle to produce nothing
            # so add a safety perimeter
            self.canvas_shape = ((2 * (pix_ra_span[1] - pix_ra_span[0]) + self.shape[0] + buffer).astype(int),
                                 (2 * (pix_dec_span[1] - pix_dec_span[0]) + self.shape[1] + buffer).astype(int))

        if force_square_grid:
            self.canvas_shape = tuple([max(self.canvas_shape)] * 2)

        # check the size of the grid is sensible. equivalent dithering along a line
        if max(self.canvas_shape) > max(self.shape) * len(dithers_data):
            getLogger(__name__).warning(f'Canvas grid {self.canvas_shape} exceeds maximum nominal extent of dithers '
                                        f'({max(self.shape) * len(dithers_data)})')
        self.canvas_wcs()
        self.canvas_header()

    def combine_metadata(self):
        """
        combine metadata from all of the h5s in a dither into a single dictionary with a MetadataSeries as the value
        for each key
        """
        combined_meta = self.dithers_data[0]['metadata'].copy()
        for entry in combined_meta:
            if not isinstance(combined_meta[entry], MetadataSeries):
                combined_meta[entry] = MetadataSeries(times=[self.dithers_data[0]['metadata']['UNIXSTR']],
                                                      values=[combined_meta[entry]])
        meta = [data['metadata'] for data in self.dithers_data[1:]]
        for md in meta:
            for entry in md:
                if not isinstance(md[entry], MetadataSeries):
                    assert isinstance(combined_meta[entry], MetadataSeries)
                    combined_meta[entry].add(md['UNIXSTR'], md[entry])
                else:
                    combined_meta[entry] += md[entry]
        return combined_meta

    def canvas_wcs(self):
        self.wcs = wcs.WCS(naxis=2)
        self.wcs.wcs.crpix = np.array(self.canvas_shape) / 2
        self.wcs.wcs.crval = [self.center.ra.deg, self.center.dec.deg]
        self.wcs.wcs.ctype = ["RA--TAN", "DEC-TAN"]
        self.wcs.pixel_shape = self.canvas_shape
        self.wcs.wcs.pc = np.eye(2)
        self.wcs.wcs.cdelt = [self.vPlateScale, self.vPlateScale]
        self.wcs.wcs.cunit = ["deg", "deg"]

        import pickle
        try:
            pickle.dumps(self.wcs)
        except Exception:
            pass
        getLogger(__name__).debug(self.wcs)

    def canvas_header(self):
        """
        Creates the header for the drizzled data. Uses combined metadata and adds the necessary drizzler keys and wcs
        solution keys
        """
        meta = self.metadata.copy()
        for key, series in self.metadata.items():
            if not len(series.values):
                meta.pop(key)
                continue
            elif all(elem == series.values[0] for elem in series.values):
                val = series.values[0]
            else:
                try:
                    val = np.mean(series.values)
                except TypeError:
                    getLogger(__name__).info(f'Cannot find mean value for key {key}, using value at the start of the'
                                             f' dither')
                    val = series.values[0]
            meta[key] = val
        self.header = mkidcore.metadata.build_header(meta, unknown_keys='warn')
        # TODO massage relevant params (i.e. UNIX timestamps should not be average)

    def write(self, filename, overwrite=True, compress=False, dashboard_orient=False, cube_type=None,
              time_bin_edges=None, wvl_bin_edges=None):
        if self.stack and dashboard_orient:
            getLogger(__name__).info('Transposing image stack to match dashboard orientation')
            getLogger(__name__).warning('Has not been verified')
            self.data = np.transpose(self.data, (0, 2, 1))
            self.wcs.pc = self.wcs.pc.T

        science_header = self.wcs.to_header()
        science_header['WCSTIME'] = (self.drizzle_params.wcs_timestep, '')
        science_header['PIXFRAC'] = (self.drizzle_params.pixfrac, '')

        if cube_type is None:
            bin_hdu = []
        elif cube_type == 'time':
            tmp = time_bin_edges
            bin_hdu = [fits.TableHDU.from_columns(np.recarray(shape=time_bin_edges.shape, buf=tmp,
                                                  dtype=np.dtype([('edges', time_bin_edges.dtype)])), name='CUBE_EDGES')]
            bin_hdu[0].header.append(fits.Card('UNIT', 's', comment='Bin unit'))
        elif cube_type == 'wave':
            tmp = wvl_bin_edges
            bin_hdu = [fits.TableHDU.from_columns(np.recarray(shape=wvl_bin_edges.shape, buf=tmp,
                                                  dtype=np.dtype([('edges', wvl_bin_edges.dtype)])), name='CUBE_EDGES')]
            bin_hdu[0].header.append(fits.Card('UNIT', 'nm', comment='Bin unit'))
        elif cube_type == 'both':
            wvl_hdu = [fits.TableHDU.from_columns(np.recarray(shape=wvl_bin_edges.shape, buf=wvl_bin_edges,
                                                  dtype=np.dtype([('edges', wvl_bin_edges.dtype)])), name='CUBE_EDGES')]
            wvl_hdu[0].header.append(fits.Card('UNIT', 'nm', comment='Bin unit'))
            time_hdu = [fits.TableHDU.from_columns(np.recarray(shape=time_bin_edges.shape, buf=time_bin_edges,
                                                              dtype=np.dtype([('edges', time_bin_edges.dtype)])),
                                                  name='CUBE_EDGES')]
            time_hdu[0].header.append(fits.Card('UNIT', 's', comment='Bin unit'))
            bin_hdu = time_hdu + wvl_hdu

        hdul = fits.HDUList([fits.PrimaryHDU(header=self.header),
                             fits.ImageHDU(name='cps', data=self.cps, header=science_header),
                             fits.ImageHDU(name='variance', data=self.counts, header=science_header)] + bin_hdu)
        if compress:
            filename = filename + '.gz'

        assert filename[-5:] == '.fits', 'Please enter valid filename'

        hdul.writeto(filename, overwrite=overwrite)
        getLogger(__name__).info('FITS file {} saved'.format(filename))

class Drizzler(Canvas):
    """
    Generate a 2D-4D hypercube from a set dithered dataset. The cube size is ntimes * ndithers * nwvlbins * nPixRA * nPixDec.
    """

    def __init__(self, dithers_data, drizzle_params, wvl_bin_width=0.0*u.nm, time_bin_width=0.0, wvl_min=700.0*u.nm,
                 wvl_max=1500*u.nm):
        super().__init__(dithers_data, drizzle_params=drizzle_params, canvas_shape=drizzle_params.canvas_shape)
        self.drizzle_params = drizzle_params
        self.pixfrac = drizzle_params.pixfrac
        self.time_bin_width = time_bin_width
        wvl_span = wvl_max.to(u.nm).value - wvl_min.to(u.nm).value

        #get wavelength bins to use
        if wvl_bin_width.to(u.nm).value > wvl_span:
            getLogger(__name__).info('Wavestep larger than entire wavelength range - using whole wavelength range '
                                     'instead')
            self.wvl_bin_edges = np.array([wvl_min.to(u.nm).value, wvl_max.to(u.nm).value])
        elif wvl_bin_width.value !=0 and wvl_span % wvl_bin_width.to(u.nm).value != 0:
            mod = wvl_span % wvl_bin_width.to(u.nm).value
            use_max = wvl_max.to(u.nm).value - mod
            n_steps = (use_max - wvl_min.to(u.nm).value)/wvl_bin_width.to(u.nm).value
            getLogger(__name__).warning(f'Specified wavelength range not evenly divisible by wavestep, using {n_steps} '
                                        f'wavelength steps of size {wvl_bin_width}')
            self.wvl_bin_edges = np.arange(wvl_min.to(u.nm).value, use_max, wvl_bin_width.to(u.nm).value)
        else:
            self.wvl_bin_edges = np.arange(wvl_min.to(u.nm).value, wvl_max.to(u.nm).value, wvl_bin_width.to(u.nm).value) if \
                wvl_bin_width.to(u.nm).value != 0.0 else np.array([wvl_min.to(u.nm).value, wvl_max.to(u.nm).value])
        startt = drizzle_params.startt
        #get time bins to use
        if startt + time_bin_width >= (drizzle_params.inttime * len(self.dithers_data)):
            getLogger(__name__).info('Timestep larger than entire duration - using whole duration instead')
            self.timebins = np.array([startt, drizzle_params.inttime])
        elif time_bin_width != 0 and (startt + drizzle_params.inttime) % time_bin_width != 0:
            mod = (startt + drizzle_params.inttime) % time_bin_width
            inttime = (startt + drizzle_params.inttime) - mod
            n_steps = inttime/time_bin_width
            getLogger(__name__).warning(f'Specified duration not evenly divisible by timestep, using {n_steps} '
                                        f'time steps of length {time_bin_width}s for each dither position ')
            self.timebins = np.append(np.arange(startt, inttime, time_bin_width), inttime)
        else:
            self.timebins = np.append(np.arange(startt, startt + drizzle_params.inttime,
                                                time_bin_width if time_bin_width!=0 else drizzle_params.inttime),
                                      startt + drizzle_params.inttime)

        self.wcs_times = np.append(np.arange(startt, self.timebins[-1], drizzle_params.wcs_timestep),
                                   self.timebins[-1])
        self.cps = None
        self.counts = None
        self.expmap = None

    def run(self, apply_weight=True):
        tic = time.clock()

        nexp_time = len(self.timebins) - 1
        nwvls = len(self.wvl_bin_edges) - 1
        ndithers = len(self.dithers_data)

        # use exp_timestep for final spacing
        # TODO this looks like it might be backwards from docs in ra/dec of canvas shape
        self.cps = np.zeros((nexp_time * ndithers, nwvls) + self.canvas_shape[::-1])
        expmap = np.zeros((nexp_time * ndithers, nwvls) + self.canvas_shape[::-1])
        for pos, dither_photons in enumerate(self.dithers_data):  # iterate over dithers

            dithhyper = np.zeros((nexp_time, nwvls) + self.canvas_shape[::-1], dtype=np.float32)
            dithexp = np.zeros((nexp_time, nwvls) + self.canvas_shape[::-1], dtype=np.float32)

            for t, inwcs in enumerate(dither_photons['obs_wcs_seq']):  # iterate through each of the wcs time spacing
                if t >= len(self.wcs_times) - 1:
                    break
                # inwcs = wcs.WCS(header=inwcs)
                inwcs.pixel_shape = self.shape

                # the sky grid ref and dither ref should match (crpix varies between dithers)
                if not np.all(np.round(inwcs.wcs.crval, decimals=4) == np.round(self.wcs.wcs.crval, decimals=4)):
                    getLogger(__name__).critical('sky grid ref and dither ref do not match '
                                                 '(crpix varies between dithers)!')
                    raise RuntimeError('sky grid ref and dither ref do not match (crpix varies between dithers)!')

                if len(self.timebins) <= len(self.wcs_times):
                    time_bins = np.array([self.wcs_times[t], self.wcs_times[t + 1]])
                else:
                    idx = np.where((self.timebins >= self.wcs_times[t]) & (self.timebins <= self.wcs_times[t+1]))
                    time_bins = self.timebins[idx]
                counts = self.make_cube(dither_photons, time_bins, self.wvl_bin_edges, applyweights=apply_weight)
                expin = time_bins[1]-time_bins[0]
                cps = counts / expin  # scale this frame by its exposure time

                # get exposure bin of current wcs time
                wcs_time = self.wcs_times[t]
                iwcs = np.where([(wcs_time >= self.timebins[i]) & (wcs_time < self.timebins[i + 1]) for i in
                               range(len(self.timebins) - 1)])[0][0]
                for it in range(len(time_bins) - 1):  # iterate over time step - > 1 if timestep < wcs_timestep
                    for n_wvl in range(nwvls):  # iterate over wavelengths
                        # create a new drizzle object for each time (and wavelength) frame
                        driz = stdrizzle.Drizzle(outwcs=self.wcs, pixfrac=self.pixfrac)
                        inwht = cps[it, n_wvl].astype(bool).astype(int)
                        driz.add_image(cps[it, n_wvl], inwcs, expin=expin, inwht=inwht, in_units='cps')
                        # for a single wcs timestep
                        dithhyper[iwcs + it, n_wvl, :, :] += driz.outsci   # sum all counts in same exposure bin
                        dithexp[iwcs + it, n_wvl, :, :] += driz.outexptime
            # for the whole dither pos
            if len(self.wcs_times) > len(self.timebins):
                wcs_per_timebin = (len(self.wcs_times) - 1) / nexp_time
                dithhyper = dithhyper/wcs_per_timebin
            self.cps[pos * nexp_time: (pos + 1) * nexp_time] = dithhyper
            expmap[pos * nexp_time: (pos + 1) * nexp_time] = dithexp

        getLogger(__name__).debug(f'Image load done in {time.clock() - tic:.1f} s')
        if nexp_time == 1 and self.time_bin_width == 0:
            self.cps = np.sum(self.cps, axis=0)
            expmap = np.sum(expmap, axis=0)
        if nwvls == 1:
            self.cps = np.squeeze(self.cps)
            expmap = expmap[0,:,:] if nexp_time == 1 else expmap[:,0,:,:]
        self.generate_header(wave=nwvls!=1, time=nexp_time!=1)
        self.counts = self.cps * expmap

    def make_cube(self, dither_photons, time_bins, wvl_bins, applyweights=False, max_counts_cut=None):
        """
        Creates a 4D image cube for the duration of the wcs timestep range or finer sampled if timestep is
        shorter
        :param dither_photons:
        :param time_bins:
        :param wvl_bins:
        :param applyweights:
        :param max_counts_cut:
        :return:
        """
        time_bins = time_bins*1e6
        weights = dither_photons['weight'] if applyweights else None
        timespan_mask = ((dither_photons['timestamps'] >= time_bins[0]) &
                         (dither_photons['timestamps'] <= time_bins[-1]))
        if weights is not None:
            weights = weights[timespan_mask]
        sample = np.vstack((dither_photons['timestamps'][timespan_mask],
                            dither_photons['wavelengths'][timespan_mask],
                            dither_photons['photon_pixels'][0][timespan_mask],
                            dither_photons['photon_pixels'][1][timespan_mask]))

        bins = np.array([time_bins, wvl_bins, range(self.shape[1] + 1), range(self.shape[0] + 1)])
        hypercube, _ = np.histogramdd(sample.T, bins, weights=weights)

        if max_counts_cut:
            getLogger(__name__).debug("Applying max pixel count cut")
            hypercube *= np.int_(hypercube < max_counts_cut)
        return hypercube

    def generate_header(self, wave=True, time=True):
        """
        Add to the extra elements to the header

        Its not clear how to increase the number of dimensions of a 2D wcs.WCS() after its created so just create
        a new object, read the original parameters where needed, and overwrite

        :return:
        """
        if wave and time:
            w = wcs.WCS(naxis=4)
            w.wcs.crpix = [self.wcs.wcs.crpix[0], self.wcs.wcs.crpix[1], 1, 1]
            w.wcs.crval = [self.wcs.wcs.crval[0], self.wcs.wcs.crval[1], self.wvl_bin_edges[0] / 1e9,
                             self.timebins[0]]
            w.wcs.ctype = [self.wcs.wcs.ctype[0], self.wcs.wcs.ctype[1], "WAVE", "TIME"]
            w.pixel_shape = (self.wcs.pixel_shape[0], self.wcs.pixel_shape[1], len(self.wvl_bin_edges) - 1 ,
                               len(self.timebins) - 1)
            w.wcs.pc = np.eye(4)
            w.wcs.cdelt = [self.wcs.wcs.cdelt[0], self.wcs.wcs.cdelt[1],
                             (self.wvl_bin_edges[1] - self.wvl_bin_edges[0]) / 1e9,
                             (self.timebins[1] - self.timebins[0])]
            w.wcs.cunit = [self.wcs.wcs.cunit[0], self.wcs.wcs.cunit[1], "m", "s"]

            self.wcs = w
            getLogger(__name__).debug('4D wcs {}'.format(w))
        elif (wave and not time) or (time and not wave):
            if wave:
                type = "WAVE"
                val =  self.wvl_bin_edges[0] / 1e9
                shape = len(self.wvl_bin_edges) - 1
                delt = (self.wvl_bin_edges[1] - self.wvl_bin_edges[0]) / 1e9
                unit = "m"
            if time:
                type = "TIME"
                val = self.timebins[0]
                shape = len(self.timebins) - 1
                delt = (self.timebins[1] - self.timebins[0])
                unit = "s"
            w = wcs.WCS(naxis=3)
            w.wcs.crpix = [self.wcs.wcs.crpix[0], self.wcs.wcs.crpix[1], 1]
            w.wcs.crval = [self.wcs.wcs.crval[0], self.wcs.wcs.crval[1],val]
            w.wcs.ctype = [self.wcs.wcs.ctype[0], self.wcs.wcs.ctype[1], type]
            w.pixel_shape = (self.wcs.pixel_shape[0], self.wcs.pixel_shape[1], shape)
            w.wcs.pc = np.eye(3)
            w.wcs.cdelt = [self.wcs.wcs.cdelt[0], self.wcs.wcs.cdelt[1], delt]
            w.wcs.cunit = [self.wcs.wcs.cunit[0], self.wcs.wcs.cunit[1], unit]
            self.wcs = w
            getLogger(__name__).debug('3D wcs {}'.format(w))
        else:
            w = wcs.WCS(naxis=2)
            w.wcs.crpix = [self.wcs.wcs.crpix[0], self.wcs.wcs.crpix[1]]
            w.wcs.crval = [self.wcs.wcs.crval[0], self.wcs.wcs.crval[1]]
            w.wcs.ctype = [self.wcs.wcs.ctype[0], self.wcs.wcs.ctype[1]]
            w.pixel_shape = (self.wcs.pixel_shape[0], self.wcs.pixel_shape[1])
            w.wcs.pc = np.eye(2)
            w.wcs.cdelt = [self.wcs.wcs.cdelt[0], self.wcs.wcs.cdelt[1]]
            w.wcs.cunit = [self.wcs.wcs.cunit[0], self.wcs.wcs.cunit[1]]
            self.wcs = w
            getLogger(__name__).debug('4D wcs {}'.format(w))



def debug_dither_image(dithers_data, drizzle_params, weight=True):
    """ Plot the location of frames with simple boxes for calibration/debugging purposes. """

    drizzle_params.canvas_shape = 500, 500  # hand set to large number to ensure all frames are captured
    driz = Drizzler(dithers_data, drizzle_params)
    driz.run(apply_weight=weight)

    fig, axes = plt.subplots(1, 2, figsize=(12, 6))
    axes[0].imshow(driz.cps, cmap='viridis', origin='lower', norm=LogNorm())

    output_image = np.zeros_like(driz.cps)
    canvas_wcs = driz.wcs
    shape = driz.shape
    del driz

    for pos, dither_photons in enumerate(dithers_data):
        # make a new driz object so the color of each frame is uniform
        driz = stdrizzle.Drizzle(outwcs=canvas_wcs, wt_scl='')
        for t, inwcs in enumerate(dither_photons['obs_wcs_seq']):
            # inwcs = wcs.WCS(header=inwcs)
            inwcs.pixel_shape = shape
            image = np.zeros(shape[::-1])  # create a simple image consisting of the array boarder and the diagonals
            image[[0, -1]] = 1
            image[:, [0, -1]] = 1
            eye = np.eye(*shape[::-1]).astype(bool)
            image[eye] = 1
            image[eye[::-1]] = 1

            driz.add_image(image, inwcs)
            driz.outsci = driz.outsci.astype(bool)
            output_image[driz.outsci] = pos

    output_image[output_image == 0] = np.nan

    axes[0].grid(True, color='k', which='both', axis='both')
    im = axes[1].imshow(output_image, cmap='Reds', origin='lower')
    divider = make_axes_locatable(axes[1])
    axes[1].grid(True, color='k', which='both', axis='both')
    cax = divider.append_axes("right", size="5%", pad=0.05)
    clb = fig.colorbar(im, cax=cax)
    clb.ax.set_title('Dither index')

    plt.show(block=True)

def mp_worker(file, startw, stopw, startt, intt, derotate, wcs_timestep, md, single_pa_time=None, exclude_flags=()):
    """
    Uses photontable.query to retrieve all photons in startw-stopw after startt for duration intt.

    startt is assumed to be in relative seconds.

    Culls photons affected by exclude_flags.

    Determines WCS solump_workertions for the interval at wcs_timestep cadence calling with derotate and single_pa_time"""
    getLogger(__name__).debug(f'Fetching data from {file}')
    pt = Photontable(file)
    photons = pt.query(startw=startw, stopw=stopw, start=startt, intt=intt)
    num_unfiltered = len(photons)
    if not len(photons):
        getLogger(__name__).warning(f'No photons found using wavelength range {startw}-{stopw} nm and time range '
                                    f'{startt}-{intt} s. Is the photontable not wavelength calibrated causing a mismatch '
                                    f'in the units?')
    else:
        getLogger(__name__).info("Fetched {} photons from dither {}".format(len(photons), file))

    exclude_flags += EXCLUDE
    photons = pt.filter_photons_by_flags(photons, disallowed=exclude_flags)
    getLogger(__name__).info(f"Removed {num_unfiltered - len(photons)} photons "
                             f"from {num_unfiltered} total from bad pix")
    xy = pt.xy(photons)

    wcs_times = pt.start_time + np.arange(startt, startt+intt, wcs_timestep)  #This is in unixtime
    wcs = pt.get_wcs(derotate=derotate, sample_times=wcs_times, single_pa_time=single_pa_time)

    del pt
    return {'file': file, 'timestamps': photons["time"], 'wavelengths': photons["wavelength"],
            'weight': photons['weight'], 'photon_pixels': xy, 'obs_wcs_seq': wcs, 'duration': intt, 'metadata': md}


def load_data(dither, wvl_min, wvl_max, startt, duration, wcs_timestep, derotate=True, align_start_pa=False, ncpu=1,
              exclude_flags=()):
    """
    Load the photons either by querying the photontables in parrallel or loading from pkl if it exists. The wcs
    solutions are added to this photon data dictionary but will likely be integrated into photontable.py directly

    startt must be relative to the start of the file in seconds

    derotate takes precedence over align_start_pa
    """
    begin = time.time()
    filenames = [o.h5 for o in dither.obs]
    meta = [o.metadata for o in dither.obs]
    if not filenames:
        getLogger(__name__).info('No photontables found')

    offsets = [o.start - int(o.start) for o in dither.obs]  # How many seconds into the h5 does valid data start
    single_pa_time = Photontable(filenames[0]).start_time if align_start_pa and not derotate else None

    if ncpu < 2:
        dithers_data = []
        for file, offset, md in zip(filenames, offsets, meta):
            data = mp_worker(file, wvl_min, wvl_max, startt + offset, duration, derotate, wcs_timestep, md,
                             single_pa_time, exclude_flags)
            dithers_data.append(data)
    else:
        #TODO result of mp_worker too big, causes issues with multiprocessing when pickling
        p = mp.Pool(ncpu)
        processes = [p.apply_async(mp_worker, (file, wvl_min, wvl_max, startt + offsett, duration, derotate,
                                               wcs_timestep, md, single_pa_time, exclude_flags))
                     for file, offsett, md in zip(filenames, offsets, meta)]
        dithers_data = [res.get() for res in processes]

    dithers_data.sort(key=lambda k: filenames.index(k['file']))

    getLogger(__name__).debug(f'Loading data took {time.time() - begin:.0f} s')

    return dithers_data


def form(dither, mode='drizzler', derotate=True, wave_start=None, wave_stop=None, start=0, duration=None, pixfrac=.5,
         wvl_bin_width=0.0*u.nm, time_bin_width=0.0, wcs_timestep=1., usecache=True, ncpu=None,
         exclude_flags=PROBLEM_FLAGS + EXCLUDE, whitelight=False, align_start_pa=False, debug_dither_plot=False,
         output_file='', weight=False):
    """
    Takes in a MKIDDitherDescription object and drizzles each frame onto a common sky grid.
    :param dither: MKIDDitherDescription, contains the lists of observations and metadata for a set of dithers
    :param mode: 'drizzler' only currently accepted mode
    :param derotate: If True, all dithers (and integrations within) are rotated to their orientation on sky. If False
    only the device angle rotation will be subtracted from each frame
    :param wave_start: start wavelength. See photontable.query()
    :param wave_stop: stop wavelength. See photontable.query()
    :param start: start offset (in seconds) for photons used in each dither.
    :param duration: upper bound on the photons used in each dither. See photontable.query()
    :param pixfrac: pixfrac parameter used in drizzle algorithm. See stsci.drizzle()
    :param wvl_bin_width: astropy.units.Quantity - size of wavelength bins. If 0.0 will use full wavelength extent of
    the instrument
    :param time_bin_width: size of time bins (in seconds). If 0.0 will use full duration
    :param wcs_timestep: Time between different wcs parameters (eg orientations). 0 will use the calculated
    non-blurring min
    :param usecache: True means the output of load_data() is stored and reloaded for subsequent runs of form
    :param ncpu: Number of cpu used when loading and reformatting the dither photontables
    :param exclude_flags: List of pixelflags to be excluded from analysis
    :param whitelight: Relevant parameters are updated to perform a whitelight dither. Takes presedence over derotate
    user input
    :param align_start_pa: If derotate is False then the first image can be oriented to its on sky position and that
    same rotation offset applied to all subsequent frames. Useful for the purpose of ADI.
    :param debug_dither_plot: Plot the location of frames with simple boxes for calibration/debugging purposes
    :param output_file: Name of the output save file
    :param weight: If True will apply weight column of the photontable to the dither frames
    :returns: drizzle : DrizzledData. Contains maps and metadata from the drizzled data
    """

    dcfg = mkidpipeline.config.PipelineConfigFactory(step_defaults=dict(drizzler=StepConfig()), ncpu=ncpu, cfg=None,
                                                     copy=True)
    ncpu = mkidpipeline.config.n_cpus_available(max=dcfg.get('ncpu', inherit=True))

    if mode not in ('drizzle'):
        raise ValueError('mode must be: drizzle')

    # ensure the user input is shorter than the dither or that wcs are just calculated for the requested timespan
    dither_inttime = min(dither.inttime)
    if duration > dither_inttime:
        getLogger(__name__).info(f'User integration time of {duration:.1f} too long, '
                                 f'using shortest dither dwell time ({dither_inttime:.1f} s)')
        used_inttime = dither_inttime
    else:
        getLogger(__name__).info(f'Using user specified integration time of {duration:.1f} s')
        used_inttime = duration

    if whitelight:
        getLogger(__name__).warning('Changing some of the wcs params to white light mode')
        derotate = False
        dither.ra = 0
        dither.dec = 0

    getLogger(__name__).debug('Parsing Params')
    drizzle_params = DrizzleParams(dither, used_inttime, wcs_timestep, pixfrac, startt=start)

    getLogger(__name__).debug('Loading data')
    dithers_data = None
    if usecache:
        settings = (tuple(o.h5 for o in dither.obs), dither.name, wave_start.value, wave_stop.value, start,
                    drizzle_params.inttime, drizzle_params.wcs_timestep, derotate, exclude_flags, align_start_pa)
        setting_hash = hashlib.md5(str(settings).encode()).hexdigest()
        pkl_save = os.path.join(mkidpipeline.config.config.paths.tmp,
                                f'drizzler_{getpass.getuser()}_{dither.name}_{setting_hash}.pkl')

        if dcfg.drizzler.clearcache:
            getLogger(__name__).info('Clearing drizzler cache')
            for f in glob(os.path.join(mkidpipeline.config.config.paths.tmp, f'drizzler_{getpass.getuser()}_*.pkl')):
                try:
                    os.remove(f)
                except IOError:
                    getLogger(__name__).error(f'Unable to remove {f} from cache')
        else:
            try:
                with open(pkl_save, 'rb') as f:
                    dithers_data = pickle.load(f)
                    getLogger(__name__).info(f'Using cached data {pkl_save}')
            except IOError:
                pass

    if dithers_data is None:
        dithers_data = load_data(dither, wave_start, wave_stop, start, drizzle_params.inttime,
                                 drizzle_params.wcs_timestep, derotate=derotate,
                                 ncpu=ncpu, exclude_flags=exclude_flags, align_start_pa=align_start_pa)
        if usecache:
            try:
                with open(pkl_save, 'wb') as handle:
                    getLogger(__name__).info(f'Saved data cache to {pkl_save}')
                    pickle.dump(dithers_data, handle, protocol=pickle.HIGHEST_PROTOCOL)
            except IOError:
                getLogger(__name__).warning(f'Unable to write cache {pkl_save}', exc_info=True)

    total_photons = sum([len(dither_data['timestamps']) for dither_data in dithers_data])

    if not total_photons:
        getLogger(__name__).critical('No photons found in any of the dithers. Check your wavelength and time ranges')
        return None

    if debug_dither_plot:
        getLogger(__name__).debug('Generating debug image')
        debug_dither_image(dithers_data, drizzle_params)

    getLogger(__name__).debug('Initializing drizzler core')
    getLogger(__name__).debug('Running Drizzler')
    driz = Drizzler(dithers_data, drizzle_params, wvl_bin_width=wvl_bin_width, time_bin_width=time_bin_width,
                    wvl_min=wave_start, wvl_max=wave_stop)
    if time_bin_width != 0.0 and wvl_bin_width != 0.0*u.nm:
        cube_type = 'both'
        time_bin_edges = np.append(np.arange(start, duration, time_bin_width), duration)
        wvl_bin_edges = np.append(np.arange(wave_start.to(u.nm).value, wave_stop.to(u.nm).value,
                                        wvl_bin_width.to(u.nm).value), wave_stop.to(u.nm).value)
    elif time_bin_width != 0.0:
        cube_type = 'time'
        time_bin_edges = np.append(np.arange(start, duration, time_bin_width), duration)
        wvl_bin_edges = None
    elif wvl_bin_width != 0.0*u.nm:
        cube_type = 'wave'
        wvl_bin_edges = np.append(np.arange(wave_start.to(u.nm).value, wave_stop.to(u.nm).value,
                                        wvl_bin_width.to(u.nm).value), wave_stop.to(u.nm).value)
        time_bin_edges = None
    else:
        cube_type = None
        wvl_bin_edges = None
        time_bin_edges = None
    getLogger(__name__).debug('Drizzling...')
    driz.run(apply_weight=weight)
    if mode == 'list':
        getLogger(__name__).debug('Writing List Drizzler Tables...')
        driz.write_list(file = output_file)
    elif output_file and mode != 'list':
        getLogger(__name__).debug('Writing fits...')
        print('CUBE TYPE IS ' + str(cube_type))
        driz.write(output_file, cube_type=cube_type, time_bin_edges=time_bin_edges, wvl_bin_edges=wvl_bin_edges)
    getLogger(__name__).info('Finished')
    return driz
"""
Author: Sarah Steiger    Date: April 1, 2020

Loads a standard spectrum from and convolves and rebind it ot match the MKID energy resolution and bin size. Then
generates an MKID spectrum of the object by performing photometry (aperture or PSF) on the MKID image. Finally
divides the flux values for the standard by the MKID flux values for each bin to get a calibration curve.

Assumes h5 files are wavelength calibrated, and they should also first be flatcalibrated and linearity corrected
(deadtime corrected)
"""
import sys,os
import scipy.constants as c
from astropy import units as u
import urllib.request as request
from urllib.error import URLError
import shutil
from contextlib import closing
from astroquery.sdss import SDSS
import astropy.coordinates as coord
from specutils import Spectrum1D
import matplotlib.gridspec as gridspec
import numpy as np
from mkidcore.corelog import getLogger
from mkidcore import pixelflags
import mkidpipeline
import matplotlib.pyplot as plt
from mkidpipeline.utils.resampling import rebin
from mkidpipeline.utils.fitting import fitBlackbody
from mkidpipeline.utils.smoothing import gaussian_convolution
from mkidpipeline.utils.interpolating import interpolate_image
from mkidpipeline.utils.photometry import get_aperture_radius, aper_photometry, astropy_psf_photometry,\
    mec_measure_satellite_spot_flux
from mkidpipeline.steps.drizzler import form as form_drizzle

_loaded_solutions = {}


class StepConfig(mkidpipeline.config.BaseStepConfig):
    yaml_tag = u'!spectralcal_cfg'
    REQUIRED_KEYS = (('photometry_type', 'aperture', 'aperture | psf'),
                     ('plots', 'summary', 'summary | none'),
                     ('interpolation', 'linear', ' linear | cubic | nearest'))


FLAGS = pixelflags.FlagSet.define(
        ('inf_weight', 1, 'Spurious infinite weight was calculated - weight set to 1.0'),
        ('lz_weight', 2, 'Spurious less-than-or-equal-to-zero weight was calculated - weight set to 1.0'),
        ('nan_weight', 4, 'NaN weight was calculated.'),
        ('below_range', 8, 'Derived wavelength is below formal validity range of calibration'),
        ('above_range', 16, 'Derived wavelength is above formal validity range of calibration'),
    )

class StandardSpectrum:
    """
    replaces the MKIDStandards class from the ARCONS pipeline for MEC.
    """
    def __init__(self, save_path='', std_path=None, object_name=None, object_ra=None, object_dec=None, coords=None):
        self.save_dir = save_path
        self.object_name = object_name
        self.ra = object_ra
        self.dec = object_dec
        self.std_path = std_path
        self.coords = coords # SkyCoord object
        self.spectrum_file = None
        self.k = 5.03411259e7

    def get(self):
        """
        function which creates a spectrum directory, populates it with the spectrum file either pulled from the ESO
        catalog, SDSS catalog, a URL, or a specified path to a .txt file and returns the wavelength and flux column in the appropriate units
        :return: wavelengths (Angstroms), flux (erg/s/cm^2/A)
        """
        self.coords = get_coords(object_name=self.object_name, ra=self.ra, dec=self.dec)
        data = self.fetch_spectra()
        return data[:, 0], data[:, 1]

    def fetch_spectra(self):
        """
        called from get(), searches either a URL, ESO catalog or uses astroquery.SDSS to search the SDSS catalog. Puts
        the retrieved spectrum in a '/spectrum/' folder in self.save_dir
        :return:
        """
        if self.std_path is not None:
            try:
                data = np.loadtxt(self.std_path)
            except OSError:
                self.spectrum_file = fetch_spectra_URL(object_name=self.object_name, url_path=self.std_path,
                                                       save_dir=self.save_dir)
                data = np.loadtxt(self.spectrum_file)
            return data
        else:
            self.spectrum_file = fetch_spectra_ESO(object_name=self.object_name, save_dir=self.save_dir)
            if not self.spectrum_file:
                self.spectrum_file = fetch_spectra_SDSS(object_name=self.object_name, save_dir=self.save_dir,
                                                        coords=self.coords)
                try:
                    data = np.loadtxt(self.spectrum_file)
                    return data
                except ValueError:
                    getLogger(__name__).error(
                        'Could not find standard spectrum for this object, please find a spectrum and point to it in '
                        'the standard_path in your pipe.yml')
                    sys.exit()
            data = np.loadtxt(self.spectrum_file)
            # to convert to the appropriate units if ESO spectra
            data[:, 1] = data[:, 1] * 10**(-16)
            return data

    def counts_to_ergs(self, a):
        """
        converts units of the spectra from counts to ergs
        :return:
        """
        a[:, 1] /= (a[:, 0] * self.k)
        return a

    def ergs_to_counts(self, a):
        """
        converts units of the spectra from ergs to counts
        :return:
        """
        a[:, 1] *= (a[:, 0] * self.k)
        return a


class SpectralCalibrator:
    def __init__(self, configuration=None, solution_name='solution.npz', interpolation=None,
                 data=None, use_satellite_spots=True, obj_pos=None, wvl_bin_edges=None, aperture_radius=None,
                 wvl_start=950, wvl_stop=1375, save_path=None, platescale=10.4, std_path='', object_name=None,
                 photometry_type='aperture', summary_plot=True, ncpu=1):

        self.interpolation = interpolation
        self.use_satellite_spots = use_satellite_spots
        self.obj_pos = obj_pos
        self.solution_name = solution_name
        self.data = data
        self.wvl_bin_edges = wvl_bin_edges
        self.aperture_radius = aperture_radius
        self.wvl_start = wvl_start
        self.wvl_stop= wvl_stop
        self.save_path = save_path
        self.platescale = platescale
        self.std_path=std_path
        self.object_name=object_name
        self.photometry=photometry_type
        self.summary_plot=summary_plot
        self.cfg=configuration
        self.ncpu=ncpu
       # various spectra
        self.std = None
        self.rebin_std = None
        self.bb = None
        self.mkid = None
        self.conv = None

        self.curve = None
        self.cube = None
        self.contrast = None

        if configuration is not None:
            # load in the configuration file
            cfg = mkidcore.config.load(configuration)
            self.save_path = cfg.paths.database
            self.obj_pos = cfg.obj_pos
            self.wvl_start = cfg.instrument.minimum_wavelength
            self.wvl_stop = cfg.instrument.maximum_wavelength
            self.use_satellite_spots = cfg.use_satellite_spots
            self.wvl_bin_edges = cfg.wvl_bin_edges
            self.data = cfg.data
            self.platescale = self.data.wcscal.platescale
            self.solution = ResponseCurve(configuration=cfg, curve=self.curve, wvl_bin_edges=self.wvl_bin_edges,
                                          cube=self.cube, solution_name=self.solution_name)
            sol = mkidpipeline.steps.wavecal.Solution(cfg.wavcal)
            r, resid = sol.find_resolving_powers(cache=True)
            self.r_list = np.nanmedian(r, axis=0)
            if cfg.aperture_radius:
                self.aperture_radius = np.full(len(self.wvl_bin_edges) - 1, cfg.aperture_radius)
            self.std_path = cfg.standard_path
            self.object_name = cfg.object_name
            self.ra = [x.ra for x in self.data]
            self.dec = [x.dec for x in self.data]
            self.photometry = cfg.spectralcal.photometry_type
            self.contrast = np.zeros(len(self.wvl_bin_edges) - 1)
            self.summary_plot = cfg.spectralcal.summary_plot
            self.obj_pos = tuple(float(s) for s in cfg.obj_pos.strip("()").split(",")) \
                if cfg.obj_pos else None
            self.interpolation = cfg.spectralcal.interpolation
        else:
            pass

        self.energy_start = (c.h * c.c) / ((self.wvl_start * 10.0) * 10 ** (-10) * c.e)
        self.energy_stop = (c.h * c.c) / ((self.wvl_stop * 10.0) * 10 ** (-10) * c.e)
        self.energy_bin_width = ((self.energy_start + self.energy_stop) / 2) / (np.median(self.r_list) * 5.0)
        self.aperture_radius = np.zeros(len(self.wvl_bin_edges) - 1) if self.wvl_bin_edges else None

    def run(self, save=True, plot=None):
        try:
            getLogger(__name__).info("Loading Spectrum from MEC")
            self.load_absolute_spectrum()
            getLogger(__name__).info("Loading Standard Spectrum")
            self.load_standard_spectrum()
            getLogger(__name__).info("Calculating Spectrophotometric Response Curve")
            self.calculate_response_curve()
            self.solution = ResponseCurve(configuration=self.cfg, curve=self.curve, wvl_bin_edges=self.wvl_bin_edges,
                                          cube=self.cube, solution_name=self.solution_name)
            if save:
                self.solution.save(save_name=self.solution_name if isinstance(self.solution_name, str) else None)
            if plot or (plot is None and self.summary_plot):
                save_name = self.solution_name.rpartition(".")[0] + ".pdf"
                self.plot_summary(save_name=save_name)
        except KeyboardInterrupt:
            getLogger(__name__).info("Keyboard shutdown requested ... exiting")

    def load_absolute_spectrum(self):
        """
         Extract the MEC measured spectrum of the spectrophotometric standard by breaking data into spectral cubes
         and performing photometry (aperture or psf) on each spectral frame
         """
        getLogger(__name__).info('performing {} photometry on MEC spectrum'.format(self.photometry))
        if self.wvl_bin_edges is None:
            # TODO make sure in angstroms
            self.wvl_bin_edges = self.data.data[0].wavelength_bins(width=self.energy_bin_width,
                                                                   start=self.wvl_start,
                                                                   stop=self.wvl_stop)
        if len(self.data.data) == 1:
            hdul = self.data.data[0].get_fits(spec_weight=True, rate=True, cube_type='wave',
                                              bin_edges=self.wvl_bin_edges, bin_type='energy')
            cube = np.array(hdul['SCIENCE'].data, dtype=np.double)
        else:
            cube = []
            for wvl in range(len(self.wvl_bin_edges) - 1):
                getLogger(__name__).info('using wavelength range {} - {}'.format(self.wvl_bin_edges[wvl] / 10,
                                                                                 self.wvl_bin_edges[wvl + 1] / 10))
                drizzled = form_drizzle(self.data, mode='spatial', wvlMin=self.wvl_bin_edges[wvl] / 10,
                                        wvlMax=self.wvl_bin_edges[wvl + 1] / 10, pixfrac=0.5, wcs_timestep=1,
                                        exp_timestep=1, exclude_flags=pixelflags.PROBLEM_FLAGS, usecache=False,
                                        ncpu=self.ncpu, derotate=not self.use_satellite_spots, align_start_pa=False,
                                        whitelight=False, debug_dither_plot=False)
                getLogger(__name__).info(('finished image {}/ {}'.format(wvl + 1.0, len(self.wvl_bin_edges) - 1)))
                cube.append(drizzled.cps)
            self.cube = np.array(cube)
        n_wvl_bins = len(self.wvl_bin_edges) - 1

        wvl_bin_centers = [(a+b)/2 for a,b in zip(self.wvl_bin_edges, self.wvl_bin_edges[1::])]

        self.mkid = np.zeros((n_wvl_bins, n_wvl_bins))
        self.mkid[0] = wvl_bin_centers
        if self.use_satellite_spots:
            fluxes = mec_measure_satellite_spot_flux(self.cube, wvl_start=self.wvl_bin_edges[:-1],
                                                     wvl_stop=self.wvl_bin_edges[1:])
            self.mkid[1] = np.nanmean(fluxes, axis=1)
        else:
            if self.obj_pos is None:
                getLogger(__name__).info('No coordinate specified for the object. Performing a PSF fit '
                                         'to find the location')
                x, y, flux = astropy_psf_photometry(cube[:,:,0], 5.0)
                ind = np.where(flux == flux.max())
                self.obj_pos = (x.data.data[ind][0], y.data.data[ind][0])
                getLogger(__name__).info('Found the object at {}'.format(self.obj_pos))
            for i in np.arange(n_wvl_bins):
                # perform photometry on every wavelength bin
                frame = cube[:, :, i]
                if self.interpolation is not None:
                    frame = interpolate_image(frame, method=self.interpolation)
                rad = get_aperture_radius(wvl_bin_centers[i], self.platescale)
                self.aperture_radius[i] = rad
                obj_flux = aper_photometry(frame, self.obj_pos, rad)
                self.mkid[1][i] = obj_flux
        return self.mkid

    def load_standard_spectrum(self):
        standard = StandardSpectrum(save_path=self.save_path, std_path=self.std_path,
                                    object_name=self.object_name[0], object_ra=self.ra[0],
                                    object_dec=self.dec[0])
        std_wvls, std_flux = standard.get() # standard star object spectrum in ergs/s/Angs/cm^2
        self.std = np.hstack(std_wvls, std_flux)
        conv_wvls_rev, conv_flux_rev = self.extend_and_convolve(self.std[0], self.std[1])
        # convolved spectrum comes back sorted backwards, from long wvls to low which screws up rebinning
        self.conv = np.hstack((conv_wvls_rev[::-1], conv_flux_rev[::-1]))

        # rebin cleaned spectrum to flat cal's wvlBinEdges
        rebin_std_data = rebin(self.conv[0], self.conv[1], self.wvl_bin_edges)
        wvl_bin_centers = [(a+b)/2 for a,b in zip(self.wvl_bin_edges, self.wvl_bin_edges[1::])]

        if self.use_satellite_spots:
            for i, wvl in enumerate(wvl_bin_centers):
                self.contrast[i] = satellite_spot_contrast(wvl)
                rebin_std_data[i,1] = rebin_std_data[i,1] * self.contrast[i]
        self.rebin_std =  np.hstack(np.array(rebin_std_data[:, 0]), np.array(rebin_std_data[:, 1]))

    def extend_and_convolve(self, x, y):
        """
        BB Fit to extend standard spectrum to 1500 nm and to convolve it with a gaussian kernel corresponding to the
        energy resolution of the detector. If spectrum spans whole MKID range will just convolve with the gaussian
        """
        r = np.median(np.nanmedian(self.r_list, axis=0))
        if np.round(x[-1]) < self.wvl_stop:
            fraction = 1.0 / 3.0
            nirX = np.arange(int(x[int((1.0 - fraction) * len(x))]), self.wvl_stop)
            T, nirY = fitBlackbody(x, y, fraction=fraction, newWvls=nirX)
            if np.any(x >= self.wvl_stop):
                self.bb = np.hstack((x, y))
            else:
                wvls = np.concatenate((x, nirX[nirX > max(x)]))
                flux = np.concatenate((y, nirY[nirX > max(x)]))
                self.bb = np.hstack((wvls, flux))
            # Gaussian convolution to smooth std spectrum to MKIDs median resolution
            new_x, new_y = gaussian_convolution(self.bb[0], self.bb[1], x_en_min=self.energy_stop,
                                             x_en_max=self.energy_start, flux_units="lambda", r=r, plots=False)
        else:
            getLogger(__name__).info('Standard Spectrum spans whole energy range - no need to perform blackbody fit')
            # Gaussian convolution to smooth std spectrum to MKIDs median resolution
            std_stop = (c.h * c.c) / (self.std[0][0] * 10**(-10) * c.e)
            std_start = (c.h * c.c) / (self.std[0][-1] * 10 ** (-10) * c.e)
            new_x, new_y = gaussian_convolution(x, y, x_en_min=std_start, x_en_max=std_stop, flux_units="lambda", r=r,
                                               plots=False)
        return new_x, new_y

    def calculate_response_curve(self):
        """
        Divide the MEC Spectrum by the rebinned and gaussian convolved standard spectrum
        """
        curve_x = self.rebin_std[0]
        curve_y = self.rebin_std[1]/self.mkid
        self.curve = np.vstack((curve_x, curve_y))
        return self.curve

    def plot_summary(self, save_name='summary_plot.pdf'):
        figure = plt.figure()
        gs = gridspec.GridSpec(2, 2)
        axes_list = np.array([figure.add_subplot(gs[0, 0]), figure.add_subplot(gs[0, 1]),
                              figure.add_subplot(gs[1, 0]), figure.add_subplot(gs[1, 1])])
        axes_list[0].imshow(np.sum(self.cube, axis=0))
        axes_list[0].set_title('MKID Instrument Image of Standard', size=8)

        std_idx = np.where(np.logical_and(self.wvl_start < self.std[0], self.std[0] < self.wvl_stop))
        conv_idx = np.where(np.logical_and(self.wvl_start < self.conv[0], self.conv[0] < self.wvl_stop))

        axes_list[1].step(self.std[0][std_idx], self.std[1][std_idx], where='mid',
                          label='{} Spectrum'.format(self.object_name[0]))
        if self.bb:
            axes_list[1].step(self.bb[0], self.bb[1], where='mid', label='BB fit')
        axes_list[1].step(self.conv[0][conv_idx], self.conv[1][conv_idx], where='mid', label='Convolved Spectrum')
        axes_list[1].set_xlabel('Wavelength (A)')
        axes_list[1].set_ylabel('Flux (erg/s/cm^2)')
        axes_list[1].legend(loc='upper right', prop={'size': 6})


        axes_list[2].step(self.rebin_std[0], self.mkid, where='mid',
                          label='MKID Histogram of Object')
        axes_list[2].set_title('Object Histograms', size=8)
        axes_list[2].legend(loc='upper right', prop={'size': 6})
        axes_list[2].set_xlabel('Wavelength (A)')
        axes_list[2].set_ylabel('counts/s/cm^2/A')

        axes_list[3].plot(self.curve[0], self.curve[1])
        axes_list[3].set_title('Response Curve', size=8)
        plt.tight_layout()
        plt.savefig(save_name)
        return axes_list


class ResponseCurve:
    def __init__(self, file_path=None, curve=None, configuration=None, wvl_bin_edges=None, cube=None,
                 solution_name='spectral_solution'):
        # default parameters
        self._parse = True
        # load in arguments
        self._file_path = os.path.abspath(file_path) if file_path is not None else file_path
        self.curve = curve
        self.cfg = configuration
        self.wvl_bin_edges = wvl_bin_edges
        self.cube = cube
        # if we've specified a file load it without overloading previously set arguments
        if self._file_path is not None:
            self.load(self._file_path)
        # if not finish the init
        else:
            self.name = solution_name  # use the default or specified name for saving
            self.npz = None  # no npz file so all the properties should be set

    def save(self, save_name=None):
        """Save the solution to a file. The directory is given by the configuration."""
        if save_name is None:
            save_path = os.path.join(self.cfg.paths.database, self.name)
        else:
            save_path = os.path.join(self.cfg.paths.database, save_name)
        if not save_path.endswith('.npz'):
            save_path += '.npz'
        getLogger(__name__).info("Saving spectrophotometric response curve to {}".format(save_path))
        np.savez(save_path, curve=self.curve, wvl_bin_edges=self.wvl_bin_edges, cube=self.cube, configuration=self.cfg)
        self._file_path = save_path  # new file_path for the solution

    def load(self, file_path, file_mode='c'):
        """
        loads in a response curve from a saved npz file and sets relevant attributes
        """
        getLogger(__name__).info("Loading solution from {}".format(file_path))
        keys = ('curve', 'configuration')
        npz_file = np.load(file_path, allow_pickle=True, encoding='bytes', mmap_mode=file_mode)
        for key in keys:
            if key not in list(npz_file.keys()):
                raise AttributeError('{} missing from {}, solution malformed'.format(key, file_path))
        self.npz = npz_file
        self.curve = self.npz['curve']
        self.cfg = self.npz['configuration']
        self.wvl_bin_edges = self.npz['wvl_bin_edges']
        self.cube = self.npz['cube']
        self._file_path = file_path  # new file_path for the solution
        self.name = os.path.splitext(os.path.basename(file_path))[0]  # new name for saving
        getLogger(__name__).info("Complete")

def name_to_ESO_extension(object_name):
    """
    converts an input object name string to the standard filename format for the ESO standards catalog on their
    ftp server
    :return:
    """
    extension = ''
    for char in object_name:
        if char.isupper():
            extension = extension + char.lower()
        elif char == '+':
            extension = extension
        elif char == '-':
            extension = extension + '_'
        else:
            extension = extension + char
    return 'f{}.dat'.format(extension)

def fetch_spectra_ESO(object_name, save_dir):
    """
    fetches a standard spectrum from the ESO catalog and downloads it to self.savedir if it exists. Requires
    self.object_name to not be None
    :return:
    """
    getLogger(__name__).info('Looking for {} spectrum in ESO catalog'.format(object_name))
    ext = name_to_ESO_extension(object_name)
    path = 'ftp://ftp.eso.org/pub/stecf/standards/'
    folders = np.array(['ctiostan/', 'hststan/', 'okestan/', 'wdstan/', 'Xshooter/'])
    spectrum_file = None
    if os.path.exists(save_dir + ext):
        getLogger(__name__).info('Spectrum already loaded, will not be reloaded')
        spectrum_file = save_dir + ext
        return spectrum_file
    for folder in folders:
        try:
            with closing(request.urlopen(path + folder + ext)) as r:
                with open(save_dir + ext, 'wb') as f:
                    shutil.copyfileobj(r, f)
            spectrum_file = save_dir + ext
        except URLError:
            pass
    return spectrum_file

def fetch_spectra_SDSS(object_name, save_dir, coords):
    """
    saves a textfile in self.save_dir where the first column is the wavelength in angstroms and the second
    column is flux in erg cm-2 s-1 AA-1
    :return: the path to the saved spectrum file
    """
    if os.path.exists(save_dir + object_name + 'spectrum.dat'):
        getLogger(__name__).info('Spectrum already loaded, will not be reloaded')
        spectrum_file = save_dir + object_name + 'spectrum.dat'
        return spectrum_file
    getLogger(__name__).info('Looking for {} spectrum in SDSS catalog'.format(object_name))
    result = SDSS.query_region(coords, spectro=True)
    if not result:
        getLogger(__name__).warning('Could not find spectrum for {} at {},{} in SDSS catalog'.format(object_name, coords.ra, coords.dec))
        spectrum_file = None
        return spectrum_file
    spec = SDSS.get_spectra(matches=result)
    data = spec[0][1].data
    lamb = 10**data['loglam'] * u.AA
    flux = data['flux'] * 10 ** -17 * u.Unit('erg cm-2 s-1 AA-1')
    spectrum = Spectrum1D(spectral_axis=lamb, flux=flux)
    res = np.array([spectrum.spectral_axis, spectrum.flux])
    res = res.T
    spectrum_file = save_dir + object_name + 'spectrum.dat'
    np.savetxt(spectrum_file, res, fmt='%1.4e')
    getLogger(__name__).info('Spectrum loaded for {} from SDSS catalog'.format(object_name))
    return spectrum_file

def fetch_spectra_URL(object_name, url_path, save_dir):
    """
    grabs the spectrum from a given URL and saves it in self.savedir
    :return: the file path to the saved spectrum
    """
    if os.path.exists(save_dir + object_name + 'spectrum.dat'):
        getLogger(__name__).info('Spectrum already loaded, will not be reloaded')
        spectrum_file = save_dir + object_name + 'spectrum.dat'
        return spectrum_file
    if not url_path:
        getLogger(__name__).warning('No URL path specified')
        pass
    else:
        with closing(request.urlopen(url_path)) as r:
            with open(save_dir + object_name + 'spectrum.dat', 'wb') as f:
                shutil.copyfileobj(r, f)
        spectrum_file = save_dir + object_name + 'spectrum.dat'
        return spectrum_file

def get_coords(object_name, ra, dec):
    """
    finds the SkyCoord object given a specified ra and dec or object_name
    :return: SkyCoord object
    """
    if ra and dec:
        coords = coord.SkyCoord(ra, dec, unit=('hourangle', 'deg'))
    else:
        try:
            coords = coord.SkyCoord.from_name(object_name)
        except TimeoutError:
            coords=None
    if not coords:
        getLogger(__name__).error('No coordinates found for spectrophotometric calibration object')
    return coords

def satellite_spot_contrast(lam, ref_contrast=2.72e-3,
                            ref_wvl=1.55 * 10 ** 4):  # 2.72e-3 number from Currie et. al. 2018b
    """

    :param lam:
    :param ref_contrast:
    :param ref_wvl:
    :return:
    """
    contrast = ref_contrast * (ref_wvl / lam) ** 2
    return contrast

def load_solution(sc, singleton_ok=True):
    """sc is a solution filename string, a ResponseCurve object, or a mkidpipeline.config.MKIDSpeccalDescription"""
    global _loaded_solutions
    if not singleton_ok:
        raise NotImplementedError('Must implement solution copying')
    if isinstance(sc, ResponseCurve):
        return sc
    if isinstance(sc, mkidpipeline.config.MKIDSpeccalDescription):
        sc = sc.path
    sc = sc if os.path.isfile(sc) else os.path.join(mkidpipeline.config.config.paths.database, sc)
    try:
        return _loaded_solutions[sc]
    except KeyError:
        _loaded_solutions[sc] = ResponseCurve(file_path=sc)
    return _loaded_solutions[sc]

def fetch(dataset, config=None, ncpu=None, remake=False, **kwargs):
    solution_descriptors = dataset.speccals
    cfg = mkidpipeline.config.config if config is None else config
    for sd in dataset.wavecals:
        wavcal = sd.path
    solutions = []
    for sd in solution_descriptors:
        sf = sd.path
        if os.path.exists(sf) and not remake:
            solutions.append(load_solution(sf))
        else:
            if 'spectralcal' not in cfg:
                scfg = mkidpipeline.config.load_task_config(StepConfig())
            else:
                scfg = cfg.copy()
            try:
                scfg.register('wavcal', wavcal, update=True)
            except AttributeError:
                scfg.register('wavcal', wavcal, update=True)
            cal = SpectralCalibrator(scfg, solution_name=sf, data=sd.data[0],
                                     use_satellite_spots=sd.use_satellite_spots, obj_pos=sd.object_position,
                                     wvl_bin_edges=sd.wvl_bin_edges, aperture_radius=sd.aperture_radius,
                                     std_path=sd.standard_path, object_name=[x.target for x in sd.data[0].obs],
                                     ncpu=ncpu if ncpu else 1)
            cal.run(**kwargs)
            # solutions.append(load_solution(sf))  # don't need to reload from file
            solutions.append(cal.solution)  # TODO: solutions.append(load_solution(cal.solution))
    return solutions
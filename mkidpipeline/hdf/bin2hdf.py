#!/opt/anaconda3/envs/pipeline/bin/python
import psutil
import tempfile
import subprocess
import os
import tables
import time
import sys
import numpy as np
from multiprocessing.pool import Pool
import multiprocessing as mp
import threading
from mkidcore.headers import ObsHeader
from mkidcore.corelog import getLogger
from mkidcore.config import yaml, yaml_object
import mkidpipeline.config
import pkg_resources as pkg
from datetime import datetime
from glob import glob
import warnings
from mkidpipeline.hdf.photontable import ObsFile
import mkidcore.utils
from mkidreadout.configuration.beammap.beammap import Beammap

__DUMMY = False

BIN2HDFCONFIGTEMPLATE = ('{x} {y}\n'
                         '{datadir}\n'
                         '{starttime}\n'
                         '{inttime}\n'
                         '{beamfile}\n'
                         '1\n'
                         '{outdir}')

_datadircache = {}


def _get_dir_for_start(base, start):
    global _datadircache

    try:
        nmin = _datadircache[base]
    except KeyError:
        nights_times = glob(os.path.join(base, '*', '*.bin'))
        nights, times = np.genfromtxt(list(map(lambda s: s[len(base) + 1:-4], nights_times)),
                                      delimiter=os.path.sep, dtype=int).T
        nmin = {times[nights == n].min(): str(n) for n in set(nights)}
        _datadircache[base] = nmin

    keys = np.array(list(nmin))
    try:
        return os.path.join(base, nmin[keys[keys < start].max()])
    except ValueError:
        raise ValueError('No directory in {} found for start {}'.format(base, start))


def testbuild(cfg, index=True):
    if cfg.starttime < 1518222559:
        raise ValueError('Data prior to 1518222559 not supported without added fixtimestamps')

    from mkidpipeline.hdf.mkidbin import extract
    from mkidcore.headers import ObsFileCols, ObsHeader

    getLogger(__name__).debug('Starting build')

    photons = extract(cfg.datadir, cfg.starttime, cfg.inttime, cfg.beamfile, cfg.x, cfg.y)

    getLogger(__name__).debug('Data Extracted')

    h5file = tables.open_file(cfg.h5file, mode="a", title="MKID Photon File")
    group = h5file.create_group("/", 'Photons', 'Photon Information')
    filter = tables.Filters(complevel=1, complib='blosc', shuffle=True, bitshuffle=False,
                            fletcher32=False)
    table = h5file.create_table(group, name='PhotonTable', description=ObsFileCols,
                                title="Photon Datatable", expectedrows=photons.shape[0],
                                filters=filter)
    table.append(photons)

    getLogger(__name__).debug('Table Populated')
    if index:

        def indexer(col, index):
            if isinstance(index, bool):
                col.create_csindex()
            else:
                col.create_index(optlevel=index[1], kind=index[0])

        indexer(table.cols.Time, index)
        getLogger(__name__).debug('Time Indexed')
        indexer(table.cols.ResID, index)
        getLogger(__name__).debug('ResID Indexed')
        indexer(table.cols.Wavelength, index)
        getLogger(__name__).debug('Wavlength Indexed')
        getLogger(__name__).debug('Table Indexed')
    else:
        getLogger(__name__).debug('Skipping Index Generation')

    # group = h5file.create_group("/", 'Images', 'Image Snaps')  #todo delete?
    bmap = Beammap(cfg.beamfile, xydim=(cfg.x, cfg.y))
    group = h5file.create_group("/", 'BeamMap', 'Beammap Information', filters=filter)
    h5file.create_array(group, 'Map', bmap.residmap, 'resID map')
    h5file.create_array(group, 'Flag', bmap.flagmap, 'flag map')

    getLogger(__name__).debug('Beammap Attached')

    h5file.create_group('/', 'header', 'Header')
    headerTable = h5file.create_table('/header', 'header', ObsHeader, 'Header')
    headerContents = headerTable.row
    headerContents['isWvlCalibrated'] = False
    headerContents['isFlatCalibrated'] = False
    headerContents['isSpecCalibrated'] = False
    headerContents['isLinearityCorrected'] = False
    headerContents['isPhaseNoiseCorrected'] = False
    headerContents['isPhotonTailCorrected'] = False
    headerContents['timeMaskExists'] = False
    headerContents['startTime'] = cfg.starttime
    headerContents['expTime'] = cfg.inttime
    headerContents['wvlBinStart'] = 700
    headerContents['wvlBinEnd'] = 1500
    headerContents['energyBinWidth'] = 0.1
    headerContents['target'] = ''
    headerContents['dataDir'] = cfg.datadir
    headerContents['beammapFile'] = cfg.beamfile
    headerContents['wvlCalFile'] = ''
    headerContents.append()

    getLogger(__name__).debug('Header Attached')
    h5file.close()
    getLogger(__name__).debug('Done')


class HDFBuilder(object):
    def __init__(self, cfg, force=False, executable_path=pkg.resource_filename('mkidpipeline.hdf', 'bin2hdf')):
        self.cfg = cfg
        self.exc = executable_path
        self.done = mkidcore.utils.manager().Event()
        self.force = force

    def handle_existing(self):
        """ Handles existing h5 files, deleting them if appropriate"""
        if os.path.exists(self.cfg.h5file):
            done = self.force

            if self.force:
                getLogger(__name__).info('Remaking {} forced'.format(self.cfg.h5file))
            else:
                try:
                    done = ObsFile(self.cfg.h5file).duration >= self.cfg.inttime
                    if not done:
                        getLogger(__name__).info(('{} does not contain full duration, '
                                                  'will remove and rebuild').format(self.cfg.h5file))
                except:
                    getLogger(__name__).info(('{} presumed corrupt,'
                                              ' will remove and rebuild').format(self.cfg.h5file), exc_info=True)
            if not done:
                try:
                    os.remove(self.cfg.h5file)
                    getLogger(__name__).info('Deleted {}'.format(self.cfg.h5file))
                except FileNotFoundError:
                    pass
            else:
                getLogger(__name__).info('H5 {} already built. Remake not requested. Done.'.format(self.cfg.h5file))
                self.done.set()

    def run(self, polltime=0.1, usepytables=True, index=('ultralight',6)):
        self.handle_existing()
        if self.done.is_set():
            return

        if usepytables:
            testbuild(self.cfg, index=index)
            self.done.set()
            return

        tfile = tempfile.NamedTemporaryFile('w', suffix='.cfg', delete=False)
        self.cfg.write(tfile)
        tfile.close()

        getLogger(__name__).debug('Wrote temp file for bin2hdf {}'.format(tfile.name))

        proc = psutil.Popen((self.exc, tfile.name),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            shell=False, cwd=None, env=None, creationflags=0)
        tic = time.time()
        while True:
            try:
                out, err = proc.communicate(timeout=polltime)
                if out:
                    getLogger(__name__).info(out.decode('utf-8'))
                if err:
                    getLogger(__name__).error(err.decode('utf-8'))
            except subprocess.TimeoutExpired:
                if time.time() - tic > 5:
                    tic = time.time()
                    getLogger(__name__).debug('Waiting on bin2hdf (pid={}) for {}'.format(proc.pid, self.cfg.h5file))

            if proc.poll() is not None:
                break

        if self.exc == __file__:
            getLogger(__name__).info('Dummy run done')
            self.done.set()
            return

        postprocess(self.cfg)

        self.done.set()

        getLogger(__name__).info('Postprocessing complete, cleaning temp files')
        try:
            os.remove(tfile.name)
        except IOError:
            getLogger(__name__).debug('Failed to delete temp file {}'.format(tfile.name))



def makehdf(cfgORcfgs, maxprocs=2, polltime=.1, events=None,
            executable_path=pkg.resource_filename('mkidpipeline.hdf', 'bin2hdf')):
    """
    Run b2n2hdf on the config(s). Takes a config or iterable of configs.

    maxprocs(2) keyword may be used to specify the maximum number of processes
    polltime(.1) sets how often processes are checked for output and output logged

    events if set must be the length of the configs and each will be set wwent the process is complete
    """
    cfgs = tuple(cfgORcfgs) if isinstance(cfgORcfgs, (tuple, list, set)) else (cfgORcfgs,)

    program = executable_path if not __DUMMY else __file__

    keepconfigs = False

    nproc = min(len(cfgs), maxprocs)
    polltime = max(.01, polltime)

    tfile_dict = {}

    if events is None:
        events = {c: mkidcore.utils.manager().Event() for c in cfgs}
    else:
        events = {c: e for c, e in zip(cfgs, events)}

    for cfg, e in zip(cfgs, events):
        with tempfile.NamedTemporaryFile('w', suffix='.cfg', delete=False) as tfile:
            cfg.write(tfile)
            tfile_dict[cfg] = tfile
        getLogger(__name__).debug('Write temp file for bin2hdf {}'.format(tfile))

    things = list(cfgs)
    procs = []
    proc_map = {}
    tics = dict()
    while things:
        cfg = things.pop()
        procs.append(psutil.Popen((program, tfile_dict[cfg].name),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  shell=False, cwd=None, env=None, creationflags=0))
        proc_map[procs[-1]] = cfg
        tics[procs[-1]] = time.time()
        while len(procs) >= nproc:
            # TODO consider replacing with https://gist.github.com/bgreenlee/1402841
            for i, proc in enumerate(procs):
                try:
                    out, err = proc.communicate(timeout=polltime)
                    if out:
                        getLogger(__name__ + '.bin2hdf_{}'.format(i)).info(out.decode('utf-8'))
                    if err:
                        getLogger(__name__ + '.bin2hdf_{}'.format(i)).error(err.decode('utf-8'))
                except subprocess.TimeoutExpired:
                    if time.time()-tics[proc] > 5:
                        tics[proc] = time.time()
                        getLogger(__name__ + '.bin2hdf_{}'.format(i)).debug('Continuing proc {} for {}'.format(
                            proc.pid,
                                                                                                                proc_map[proc].h5file))
            procs = list(filter(lambda p: p.poll() is None, procs))

    while len(procs):
        # TODO consider replacing with https://gist.github.com/bgreenlee/1402841
        for i, proc in enumerate(procs):
            try:
                out, err = proc.communicate(timeout=polltime)
                if out:
                    getLogger(__name__ + '.bin2hdf_{}'.format(i)).info(out.decode('utf-8'))
                if err:
                    getLogger(__name__ + '.bin2hdf_{}'.format(i)).error(err.decode('utf-8'))
            except subprocess.TimeoutExpired:
                if time.time() - tics[proc] > 5:
                    tics[proc] = time.time()
                    getLogger(__name__ + '.bin2hdf_{}'.format(i)).debug('Continuing proc {} for {}'.format(
                        proc.pid,proc_map[proc].h5file))
        procs = list(filter(lambda p: p.poll() is None, procs))

    if __DUMMY:
        getLogger(__name__).info('Dummy run done')
        return

    # Postprocess the h5 files
    ncore = min(nproc, len(cfgs))
    getLogger(__name__).info('Postprocessing {} H5 files using {} cores'.format(len(cfgs), ncore))
    if nproc > 1 and len(cfgs) > 1:
        pool = Pool(ncore)
        pool.map(postprocess, events.items())
    else:
        map(postprocess, events)

    getLogger(__name__).info('Postprocessing complete')

    # Clean up temp files
    if keepconfigs:
        getLogger(__name__).info('bin2hdf config files left in {}'.format(os.path.dirname(tfile_dict.values()[0])))
    else:
        getLogger(__name__).info('Cleaning temp files')
        tfiles = tfile_dict.values()
        while tfiles:
            tfile = tfiles.pop()
            try:
                os.remove(tfile.name)
            except IOError:
                getLogger(__name__).debug('Failed to delete temp file {}'.format(tfile.name))


def postprocess(cfg, event=None):
    time.sleep(.1)
    add_header(cfg)
    time.sleep(.1)
    if cfg.starttime < 1518222559:
        fix_timestamp_bug(cfg.h5file)
    time.sleep(.1)
    # Prior to Ben's speedup of bin2hdf.c the consolidatePhotonTablesCmd step would need to be here
    index_hdf(cfg)
    if event is not None:
        event.set()


def _correct_timestamps(timestamps):
    """
    Corrects errors in timestamps due to firmware bug present through PAL2017b.

    Parameters
    ----------
    timestamps: numpy array of integers
        List of timestamps from photon list. Must be in original, unsorted order.

    Returns
    -------
    Array of corrected timestamps, dtype is uint32
    """
    timestamps = np.array(timestamps, dtype=np.int64)  # convert timestamps to signed values
    photonTimestamps = timestamps % 500
    hdrTimestamps = timestamps - photonTimestamps

    unsortedInds = np.where(np.diff(timestamps) < 0)[0] + 1  # mark locations n where T(n)<T(n-1)

    for ind in unsortedInds:
        indsToIncrement = np.where(hdrTimestamps == hdrTimestamps[ind])[0]
        indsToIncrement = indsToIncrement[indsToIncrement >= ind]
        hdrTimestamps[indsToIncrement] += 500

    correctedTimestamps = hdrTimestamps + photonTimestamps

    if np.any(np.diff(correctedTimestamps) < 0):
        correctedTimestamps = _correct_timestamps(correctedTimestamps)

    return np.array(correctedTimestamps, dtype=np.uint32)


def index_hdf(cfg):
    hfile = tables.open_file(cfg.h5file, 'a')
    hfile.set_node_attr('/', 'PYTABLES_FORMAT_VERSION', '2.0')
    hfile.format_version = '2.0'
    filterObj = tables.Filters(complevel=0, complib='lzo')
    photonTable = hfile.root.Photons.PhotonTable
    photonTable.cols.Time.create_csindex(filters=filterObj)
    photonTable.cols.ResID.create_csindex(filters=filterObj)
    photonTable.cols.Wavelength.create_csindex(filters=filterObj)
    photonTable.flush()
    hfile.close()


# TODO:  This isn't in its final form, there is a patch to deal with the Bin2HDF bug
def fix_timestamp_bug(file):
    # which writes the same photonlist twice to certain resIDs
    noResIDFlag = 2 ** 32 - 1
    hfile = tables.open_file(file, mode='a')
    beamMap = hfile.root.BeamMap.Map.read()
    imShape = np.shape(beamMap)
    photonTable = hfile.get_node('/Photons/PhotonTable/')
    photonList = photonTable.read()

    resIDDiffs = np.diff(photonList['ResID'])
    if np.any(resIDDiffs < 0):
        warnings.warn('Photon list not sorted by ResID! This could take a while...')
        photonList = np.sort(photonList, order='ResID',
                             kind='mergsort')  # mergesort is stable, so time order will be preserved
        resIDDiffs = np.diff(photonList['ResID'])

    resIDBoundaryInds = np.where(resIDDiffs > 0)[
                            0] + 1  # indices in masterPhotonList where ResID changes; ie marks boundaries between pixel tables
    resIDBoundaryInds = np.insert(resIDBoundaryInds, 0, 0)
    resIDList = photonList['ResID'][resIDBoundaryInds]
    resIDBoundaryInds = np.append(resIDBoundaryInds, len(photonList['ResID']))
    correctedTimeListMaster = np.zeros(len(photonList))
    for x in range(imShape[0]):
        for y in range(imShape[1]):
            resID = beamMap[x, y]
            resIDInd0 = np.where(resIDList == resID)[0]
            if resID == noResIDFlag or len(resIDInd0) == 0:
                # getLogger(__name__).info('Table not found for pixel', x, ',', y)
                continue
            resIDInd = resIDInd0[0]
            photonList_resID = photonList[resIDBoundaryInds[resIDInd]:resIDBoundaryInds[resIDInd + 1]]
            timeList = photonList_resID['Time']
            timestamps = np.array(timeList, dtype=np.int64)  # convert timestamps to signed values
            repeatTest = np.array(np.where(timestamps == timestamps[0]))
            if len(repeatTest[0]) > 1:
                print(x, y, resID)
                timestamps1 = timestamps[0:repeatTest[0][1]]
                timestamps2 = timestamps[repeatTest[0][1]:len(timestamps)]
                correctedTimeList1 = _correct_timestamps(timestamps1).tolist()
                correctedTimeList2 = _correct_timestamps(timestamps2).tolist()
                correctedTimeList = correctedTimeList1 + correctedTimeList2
                correctedTimeList = np.array(correctedTimeList)
            else:
                correctedTimeList = _correct_timestamps(timeList)
            assert len(photonList_resID) == len(timeList), 'Timestamp list does not match length of photon list!'
            correctedTimeListMaster[resIDBoundaryInds[resIDInd]:resIDBoundaryInds[resIDInd + 1]] = correctedTimeList
    assert len(photonList) == len(correctedTimeListMaster), 'Timestamp list does not match length of photon list!'
    correctedTimeListMaster = np.array(correctedTimeListMaster).flatten()
    photonTable.modify_column(column=correctedTimeListMaster, colname='Time')
    photonTable.flush()
    hfile.close()


def add_header(cfg, wvlBinStart=700, wvlBinEnd=1500, energyBinWidth=0.1):
    dataDir = cfg.datadir
    firstSec = cfg.starttime
    expTime = cfg.inttime
    beammapFile = cfg.beamfile
    hfile = tables.open_file(cfg.h5file, mode='a')
    hfile.create_group('/', 'header', 'Header')
    headerTable = hfile.create_table('/header', 'header', ObsHeader, 'Header')
    headerContents = headerTable.row
    headerContents['isWvlCalibrated'] = False
    headerContents['isFlatCalibrated'] = False
    headerContents['isSpecCalibrated'] = False
    headerContents['isLinearityCorrected'] = False
    headerContents['isPhaseNoiseCorrected'] = False
    headerContents['isPhotonTailCorrected'] = False
    headerContents['timeMaskExists'] = False
    headerContents['startTime'] = firstSec
    headerContents['expTime'] = expTime
    headerContents['wvlBinStart'] = wvlBinStart
    headerContents['wvlBinEnd'] = wvlBinEnd
    headerContents['energyBinWidth'] = energyBinWidth
    headerContents['target'] = ''
    headerContents['dataDir'] = dataDir
    headerContents['beammapFile'] = beammapFile
    headerContents['wvlCalFile'] = ''
    headerContents.append()
    headerTable.flush()
    hfile.close()


@yaml_object(yaml)
class Bin2HdfConfig(object):
    def __init__(self, datadir='./', beamfile='./default.bmap', starttime=None, inttime=None,
                 outdir='./', x=140, y=146, writeto=None, beammap=None):

        self.datadir = datadir
        self.starttime = starttime
        self.inttime = inttime

        self.beamfile = beamfile
        self.x = x
        self.y = y

        if beammap is not None:
            self.beamfile = beammap.file
            self.x = beammap.ncols
            self.y = beammap.nrows

        self.outdir = outdir
        if writeto is not None:
            self.write(writeto)

    @property
    def h5file(self):
        return os.path.join(self.outdir, str(self.starttime) + '.h5')

    def write(self, file):
        dir = self.datadir
        if not glob(os.path.join(dir, '*.bin')):
            dir = os.path.join(self.datadir, datetime.utcfromtimestamp(self.starttime).strftime('%Y%m%d'))
        else:
            getLogger(__name__).debug('bin files found in data directory. Will not append YYYMMDD')

        try:
            file.write(BIN2HDFCONFIGTEMPLATE.format(datadir=dir, starttime=self.starttime,
                                                    inttime=self.inttime, beamfile=self.beamfile,
                                                    outdir=self.outdir, x=self.x, y=self.y))
        except AttributeError:
            with open(file, 'w') as wavefile:
                wavefile.write(BIN2HDFCONFIGTEMPLATE.format(datadir=dir, starttime=self.starttime,
                                                            inttime=self.inttime, beamfile=self.beamfile,
                                                            outdir=self.outdir, x=self.x, y=self.y))

    def load(self):
        raise NotImplementedError


def _runbuilder(b):
    getLogger(__name__).debug('Calling run on {}'.format(b.cfg.h5file))
    b.run()


def buildtables(timeranges, config=None, ncpu=1, asynchronous=False, remake=False):
    cfg = mkidpipeline.config.config if config is None else config

    timeranges = list(set(timeranges))

    b2h_configs = []
    for start_t, end_t in timeranges:
        bc = Bin2HdfConfig(datadir=_get_dir_for_start(cfg.paths.data, start_t),
                           beammap=cfg.beammap, outdir=cfg.paths.out,
                           starttime=start_t, inttime=end_t - start_t)
        b2h_configs.append(bc)

    builders = [HDFBuilder(c, force=remake) for c in b2h_configs]
    events = [b.done for b in builders]

    pool = mp.Pool(min(ncpu, mp.cpu_count()))

    if asynchronous:
        getLogger(__name__).debug('Running async on {} builders'.format(len(builders)))
        pool.map_async(_runbuilder, builders)
        return timeranges, events
    else:
        pool.map(_runbuilder, builders)


if __name__ == '__main__':
    """run as a dummy for testing"""
    print('Pretending to run bin2hdf on ' + sys.argv[1])

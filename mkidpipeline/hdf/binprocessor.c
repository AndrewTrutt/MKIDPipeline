/***********************************************************************************************
 * Bin2HDF.c - A program to convert a sequence of .bin files from the Gen2 readout into a
 *  h5 file.
 *
 * compiled with this command
 /usr/local/hdf5/bin/h5cc -shlib -pthread -O3 -g -o bin2hdf bin2hdf.c
 *************************************************************************************************/

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <stdint.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <netdb.h>
#include <stdint.h>
#include <sys/time.h>
#include <signal.h>
#include <time.h>
#include <errno.h>
#include <pthread.h>
#include <semaphore.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <math.h>
#include <dirent.h>
#include "binprocessor.h"

//max number of characters in all strings
#define STR_SIZE 200

//number of dimensions in the Variable Length array (VLarray).
//There is a 1D array of pointers to variable length arrays, so rank=1
#define DATA_RANK 1
#define NFIELD 5

// MKID array stats
#define NPIXELS_PER_ROACH 1024
#define RAD2DEG 57.2957795131

#define TSOFFS2017 1483228800 //difference between epoch and Jan 1 2017 UTC
#define TSOFFS 1514764800 //difference between epoch and Jan 1 2018 UTC

struct datapacket {
    int baseline:17;
    int wvl:18;
    unsigned int timestamp:9;
    unsigned int ycoord:10;
    unsigned int xcoord:10;
}__attribute__((packed));;

struct hdrpacket {
    unsigned long timestamp:36;
    unsigned int frame:12;
    unsigned int roach:8;
    unsigned int start:8;
}__attribute__((packed));;

// useful globals
uint32_t residarr[10000] = {0};
uint64_t tstart = 0;

void FixOverflowTimestamps(struct hdrpacket* hdr, int fileNameTime, int tsOffs) {
    int fudgeFactor = 3; //account for early starts - misalign between FirstFile and real header timestamp
    int nWraps = (fileNameTime - tsOffs - (int)(hdr->timestamp/2000) + fudgeFactor)/1048576;
    //printf("nWraps: %d\n", nWraps);
    hdr->timestamp += 2000*nWraps*1048576;
}

/*
 * Sorts all photon tables in time order. Uses insertion sort (good for mostly ordered data)
 */
void SortPhotonTables(photon ***ptable, uint32_t **ptablect, uint32_t beamCols, uint32_t beamRows) {
    photon *photonToSortAddr; //address of element currently being sorted
    photon *curPhotonAddr; //address of element being compared to photonToSort
    photon *photonSwapAddr; //address of element being moved, once correct index for photonToSort has been found
    photon photonToSort; //stores the data in photonToSortAddr
    unsigned int x,y; //beammap indices

    for(x=0; x<beamCols; x++)
        for(y=0; y<beamRows; y++)
            //loop through photons in list, check if it is greater than previous elements (all previous elements are already sorted)
            for(photonToSortAddr = ptable[x][y]+1; photonToSortAddr < ptable[x][y] + ptablect[x][y]; photonToSortAddr++){
                //check elements before photonToSort (curPhotonAddr) until correct spot is found (curPhotonAddr->timestamp < photonToSortAddr->timestamp)
                for(curPhotonAddr = photonToSortAddr-1; curPhotonAddr >= ptable[x][y]; curPhotonAddr--) {
                    if(photonToSortAddr->timestamp >= curPhotonAddr->timestamp) {
                        if(curPhotonAddr == photonToSortAddr-1) break; //this photon is already sorted
                        else {//moves photonToSort into correct position
                            photonToSort = *photonToSortAddr;
                            for(photonSwapAddr = photonToSortAddr; photonSwapAddr > curPhotonAddr+1; photonSwapAddr--)
                                *photonSwapAddr = *(photonSwapAddr-1);

                            *(curPhotonAddr+1) = photonToSort;
                            break;
                        }
                    }
                    else if(curPhotonAddr==ptable[x][y]) { //Photon is smallest in the table
                        photonToSort = *photonToSortAddr;
                        for(photonSwapAddr = photonToSortAddr; photonSwapAddr > curPhotonAddr; photonSwapAddr--)
                            *photonSwapAddr = *(photonSwapAddr-1);

                        *curPhotonAddr = photonToSort;
                        break;
                    }
                }
            }
}

long ParseBeamMapFile(const char *BeamFile, uint32_t **BeamMap, uint32_t **BeamFlag, long **DiskBeamMap) {
    // read in Beam Map file
    // format is [ResID, flag, X, Y], X is [0, 79], Y is [0, 124]
    // flags are [0,1,2+] --> [good, noDacTone, failed Beammap]

    FILE *fp;
    long ret, resid, flag, x, y;
    long count=0;
    fp = fopen(BeamFile, "r");
    printf("%s", BeamFile);
    do {
        ret = fscanf(fp,"%ld %ld %ld %ld\n", &resid, &flag, &x, &y);
        if(ret == 4) {
            DiskBeamMap[count][0] = resid;
            DiskBeamMap[count][1] = flag;
            DiskBeamMap[count][2] = x;
            DiskBeamMap[count][3] = y;
            count++;
        }
        //printf("%d %d %d %d\n", resid, flag, x, y);
        BeamMap[x][y] = resid;
        if(flag>1) BeamFlag[x][y] = 2;
        else BeamFlag[x][y] = flag;
    } while ( ret == 4);
    fclose(fp);
    return(count);
}

/*
 * Initializes all values of BeamMap to value
 */
void InitializeBeamMap(uint32_t **BeamMap, uint32_t value, uint32_t beamCols, uint32_t beamRows) {
    unsigned int x, y;
    for(x=0; x<beamCols; x++)
        for(y=0; y<beamRows; y++)
            BeamMap[x][y] = value;
}

void ParseToMem(char *packet, uint64_t l, int tsOffs, int FirstFile, int iFile, uint32_t **BeamMap, uint32_t **BeamFlag,
                int mapflag, char ***ResIdString, photon ***ptable, uint32_t **ptablect, uint32_t beamCols, uint32_t beamRows) {
    uint64_t i,swp,swp1;
    int64_t basetime;
    struct hdrpacket *hdr;
    struct datapacket *data;
    long cursize;

    // get info from header packet
    swp = *((uint64_t *) (&packet[0]));
    swp1 = __bswap_64(swp);
    hdr = (struct hdrpacket *) (&swp1);
    if (hdr->start != 0b11111111) {
        printf("Error - packet does not start with a correctly formatted header packet!\n");
        return;
    }

    // if no start timestamp, store start timestamp
    FixOverflowTimestamps(hdr, FirstFile + iFile, tsOffs); //TEMPORARY FOR 20180625 MEC - REMOVE LATER
    basetime = hdr->timestamp - tstart; // time since start of first file, in half ms
    //printf("Roach: %d; Offset: %d\n", hdr->roach, FirstFile - tsOffs - hdr->timestamp/2000);

    if( basetime < 0 ) { // maybe have some packets out of order early in file
	    printf("Early Start!\n");
		//basetime = 0;
        return;

	}

    for(i=1;i<l/8;i++) {
        //printf("i=%ld\n",i); fflush(stdout);
		swp = *((uint64_t *) (&packet[i*8]));
		swp1 = __bswap_64(swp);
		data = (struct datapacket *) (&swp1);
		if( data->xcoord >= beamCols || data->ycoord >= beamRows ) continue;
	    if( mapflag > 0 && BeamFlag[data->xcoord][data->ycoord] > 0) continue ; // if mapflag is set only record photons that were succesfully beammapped

		// When we have more than 2500 cts reallocate the memory for more
		//if( ptablect[data->xcoord][data->ycoord] > 2500*5-1 ) continue;
		if( ptablect[data->xcoord][data->ycoord] % 2500 == 2498 ) {
		    cursize = (long) ceil(ptablect[data->xcoord][data->ycoord]/2500.0);
		    //printf("cursize=%ld\n",cursize);
		    ptable[data->xcoord][data->ycoord] = (photon *) realloc(ptable[data->xcoord][data->ycoord],2500*sizeof(photon)*(cursize+1));
		}

		// add the photon to ptable and increment the appropriate counter
        ptable[data->xcoord][data->ycoord][ptablect[data->xcoord][data->ycoord]].resID = BeamMap[data->xcoord][data->ycoord];
		ptable[data->xcoord][data->ycoord][ptablect[data->xcoord][data->ycoord]].timestamp = (uint32_t) (basetime*500 + data->timestamp);
		ptable[data->xcoord][data->ycoord][ptablect[data->xcoord][data->ycoord]].wvl = ((float) data->wvl)*RAD2DEG/32768.0;
		ptable[data->xcoord][data->ycoord][ptablect[data->xcoord][data->ycoord]].wSpec = 1.0;
		ptable[data->xcoord][data->ycoord][ptablect[data->xcoord][data->ycoord]].wNoise = 1.0;
		ptablect[data->xcoord][data->ycoord]++;
    }

}


long extract_photons(const char *binpath, unsigned long start_timestamp, unsigned long integration_time,
                     const char *beammap_file, unsigned int bmap_ncol, unsigned int bmap_nrow,
                     unsigned long n_max_photons, photon* otable) {


    char fName[STR_SIZE]; //TODO this should be a malloc based on the length of binpath to prevent possibile segfault
    int FirstFile, mapflag, nRoaches;
    uint32_t beamCols, beamRows, nFiles;
    long fSize, rd, j, k, x, y;
    struct stat st;
    FILE *fp;
    clock_t start, diff, olddiff;
    uint64_t swp, swp1, i, pstart, pcount, firstHeader;
    long nPhot;
    struct hdrpacket *hdr;
    char packet[808*16];
    uint64_t *frame;
    uint32_t **BeamMap;
    uint32_t **BeamFlag;
    uint32_t *toWriteBeamMap;
    uint32_t *toWriteBeamFlag;
    uint32_t beamMapInitVal = (uint32_t)(-1);
    char ***ResIdString;
    photon ***ptable;
    uint32_t **ptablect;
    uint64_t *data;
    long **DiskBeamMap;
    long DiskBeamMapLen;

    //Timing variables
    struct tm *startTime;
    struct tm *yearStartTime; //Jan 1 00:00 UTC of current year
    int year;
    uint32_t tsOffs; //UTC timestamp for yearStartTime
    time_t startTs;

    start = clock();

    memset(packet, 0, sizeof(packet[0]) * 808 * 16);    // zero out array

	FirstFile=start_timestamp;
	nFiles=integration_time+1;
	mapflag=1;
	beamCols = bmap_ncol;
	beamRows = bmap_nrow;

	 // check whether binpath exists
    DIR* dir = opendir(binpath);
    if (ENOENT == errno) return -1;
    closedir(dir);

    // check nFiles
    printf("nFiles = %d\n", nFiles);
    if(nFiles < 1 || nFiles > 1800) return -1; // limiting number of files to 30 minutes


    startTs = (time_t)FirstFile;
    startTime = gmtime(&startTs);
    year = startTime->tm_year;
    yearStartTime = calloc(1, sizeof(struct tm));
    yearStartTime->tm_year = year;
    yearStartTime->tm_mday = 1;
    tsOffs = timegm(yearStartTime);
    tstart = (uint64_t)(FirstFile-tsOffs)*2000;

    printf("Start time = %ld\n",tstart); fflush(stdout);

    //initialize nRoaches
    nRoaches = beamRows*beamCols/1000;
    frame = (uint64_t*)malloc(nRoaches*sizeof(uint64_t));

    // Allocate memory
    // Set up memory structure for 2D "beammap" arrays
    BeamMap = (uint32_t**)malloc(beamCols * sizeof(uint32_t*));
    BeamFlag = (uint32_t**)malloc(beamCols * sizeof(uint32_t*));
    ptable = (photon***)malloc(beamCols * sizeof(photon**));
    ptablect = (uint32_t**)malloc(beamCols * sizeof(uint32_t*));
    ResIdString = (char***)malloc(beamCols * sizeof(char**));
    toWriteBeamMap = (uint32_t*)malloc(beamCols * beamRows * sizeof(uint32_t));
    toWriteBeamFlag = (uint32_t*)malloc(beamCols * beamRows * sizeof(uint32_t));
    DiskBeamMap = (long **)malloc(beamCols * beamRows * sizeof(long*));

    printf("Allocated flag maps.\n"); fflush(stdout);

    for(i=0; i<beamCols; i++) {
        BeamMap[i] = (uint32_t*)malloc(beamRows * sizeof(uint32_t));
        BeamFlag[i] = (uint32_t*)malloc(beamRows * sizeof(uint32_t));
        ptable[i] = (photon**)malloc(beamRows * sizeof(photon*));
        ptablect[i] = (uint32_t*)calloc(beamRows , sizeof(uint32_t));
        ResIdString[i] = (char**)malloc(beamRows * sizeof(char*));
        for(j=0; j<beamRows; j++) ResIdString[i][j] = (char*)malloc(20 * sizeof(char));
    }

    for(i=0; i<beamCols*beamRows; i++) DiskBeamMap[i] = (long *)calloc(4, sizeof(long));

    printf("Allocated ptable.\n"); fflush(stdout);

    // Read in beam map and parse it make 2D beam map and flag arrays
    InitializeBeamMap(BeamMap, beamMapInitVal, beamCols, beamRows); //initialize to out of bounds resID
    InitializeBeamMap(BeamFlag, 1, beamCols, beamRows); //initialize flag to one
    DiskBeamMapLen=ParseBeamMapFile(beammap_file,BeamMap,BeamFlag,DiskBeamMap);
    printf("\nParsed beam map.\n"); fflush(stdout);

    for(i=0; i < beamCols; i++) {
		for(j=0; j < beamRows; j++) {
			if( BeamMap[i][j] == 0 ) {
                printf("ResID 0 at (%d,%d)\n", i, j); fflush(stdout);
            }

            if(BeamMap[i][j] == beamMapInitVal) {
                printf("ResID N/A at (%d,%d)\n", i, j); fflush(stdout);
                continue;
            }

			ptable[i][j] = (photon *) malloc( 2500 * sizeof(photon) );	// allocate memory for ptable
		}
	}

    // put the beam map into the h5 file

    for(i=0; i<beamCols; i++) {
        for(j=0; j<beamRows; j++) {
            toWriteBeamMap[beamRows*i + j] = BeamMap[i][j];
            toWriteBeamFlag[beamRows*i + j] = BeamFlag[i][j];
        }
    }

	printf("Made individual photon data tables.\n"); fflush(stdout);

    // Loop through the data files and parse the packets into separate data tables

    for(i=0; i < nFiles; i++) {
        sprintf(fName,"%s/%ld.bin",binpath,FirstFile+i);
        stat(fName, &st);
        fSize = st.st_size;
        printf("\nReading %s - %ld Mb\n",fName,fSize/1024/1024);
        data = (uint64_t *) malloc(fSize); //#TODO this is a memory leak!!
        //dSize = (uint64_t) fSize;

        fp = fopen(fName, "rb");
        rd = fread( data, 1, fSize, fp);
        if( rd != fSize) {printf("Didn't read the entire file %s\n",fName); fflush(stdout);}
        fclose(fp);

        // parse the data into photon tables in memory
        for( j=0; j<fSize/8; j++) {
            swp = *((uint64_t *) (&data[j]));
            swp1 = __bswap_64(swp);
            hdr = (struct hdrpacket *) (&swp1);
            if (hdr->start == 0b11111111) {
                firstHeader = j;
                pstart = j;
                if( firstHeader != 0 ) { printf("First header at %ld\n",firstHeader); fflush(stdout);}
                break;
            }
        }

        // reformat all the packets into memory then dump to disk for speed
        for( k=firstHeader+1; k<(fSize/8); k++) {
            swp = *((uint64_t *) (&data[k]));
            swp1 = __bswap_64(swp);
            hdr = (struct hdrpacket *) (&swp1);

            if (hdr->start == 0b11111111) {        // found new packet header!
                //fill packet and parse
                if( k*8 - pstart > 816 ) { printf("Packet too long - %ld bytes\n",k*8 - pstart); fflush(stdout);}
                memmove(packet,&data[pstart/8],k*8 - pstart);
                pcount++;
                // add to HDF5 file
     	        ParseToMem(packet,k*8-pstart,tsOffs,FirstFile,i,BeamMap,BeamFlag,mapflag,ResIdString,ptable,ptablect,beamCols,beamRows);
		        pstart = k*8;   // move start location for next packet
		        if( pcount%1000 == 0 ) { printf("."); fflush(stdout);}
            }
        }


    }

    printf("\nSorting photon tables.\n"); fflush(stdout);
    SortPhotonTables(ptable, ptablect, beamCols, beamRows);

    diff = clock()-start;
    olddiff = diff;

    printf("Read and parsed data in memory in %f s.\n",(float)diff/CLOCKS_PER_SEC);  fflush(stdout);

    nPhot=0;
    for(j=0; j < beamCols*beamRows; j++) {
        x = DiskBeamMap[j][2];
        y = DiskBeamMap[j][3];
        if( BeamMap[x][y] == beamMapInitVal ) continue;
        if( ptablect[x][y] == 0 ) continue;
        memcpy(&otable[nPhot], ptable[x][y], ptablect[x][y] * sizeof(photon));
        nPhot +=  ptablect[x][y];
	}

	printf("Memcopy done.\n"); fflush(stdout);

	// free photon tables for every resid
    for(i=0; i < beamCols; i++) {
		for(j=0; j < beamRows; j++) {
			if( BeamMap[i][j] == 0 ) continue;
			free(ptable[i][j]);
		}
	}


    diff = clock()-start;
    printf("Parsed %ld photons in %f seconds: %9.1f photons/sec.\n",nPhot,((float)diff)/CLOCKS_PER_SEC,
        ((float)nPhot)/((float)(diff)/CLOCKS_PER_SEC)); fflush(stdout);

    free(data);

    for(i=0; i<beamCols; i++)
    {
        free(BeamMap[i]);
        free(BeamFlag[i]);
        free(ptable[i]);
        free(ptablect[i]);
        free(ResIdString[i]);
    }

    for(i=0; i<beamCols*beamRows; i++)
        free(DiskBeamMap[i]);

    free(BeamMap);
    free(BeamFlag);
    free(ptable);
    free(ptablect);
    free(ResIdString);
    free(toWriteBeamMap);
    free(toWriteBeamFlag);
    free(DiskBeamMap);

    free(yearStartTime);

    return nPhot;
}



long extract_photons_dummy(const char *binpath, unsigned long start_timestamp, unsigned long integration_time,
                     const char *beammap_file, unsigned int bmap_ncol, unsigned int bmap_nrow,
                     unsigned long n_max_photons, photon* otable) {
    int i;

    if (n_max_photons<10) {
        printf("Need at least an array of 10 to do a dummy job\n");
        fflush(stdout);
        return -1;
    }

    printf("binpath %s\nstart %ld\n int %ld \nbeammap %s\nncol %ld\nnrow %ld\nnmax %ld\n",
           binpath, start_timestamp, integration_time, beammap_file, bmap_ncol, bmap_nrow, n_max_photons);
    fflush(stdout);
    for (i=0;i<5;i++) {
        printf("photon %ld, %ld, %f, %f, %f\n", otable[i].resID, otable[i].timestamp, otable[i].wvl, otable[i].wSpec,
               otable[i].wNoise);
        fflush(stdout);
        otable[i].resID=12;
        otable[i].timestamp=13;
        otable[i].wvl=-1.0;
        otable[i].wSpec=-2.0;
        otable[i].wNoise=-3.0;
    }

    photon morephotons[3];
    morephotons[0].resID=20;
    morephotons[0].timestamp=21;
    morephotons[0].wvl=-10.;
    morephotons[0].wSpec=-10.;
    morephotons[0].wNoise=-10.0;

    morephotons[1].resID=20;
    morephotons[1].timestamp=22;
    morephotons[1].wvl=-10.;
    morephotons[1].wSpec=-12.;
    morephotons[1].wNoise=-10.0;

    morephotons[2].resID=20;
    morephotons[2].timestamp=24;
    morephotons[2].wvl=-10.;
    morephotons[2].wSpec=-13.;
    morephotons[2].wNoise=-10.0;

    memcpy(&otable[5], morephotons, 3 * sizeof(photon));

    return n_max_photons;

}
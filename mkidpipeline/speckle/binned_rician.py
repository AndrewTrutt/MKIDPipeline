#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Jun 21 11:24:00 2018

Author: Clint Bockstiegel
Date: June 21, 2018
Last Updated: August 10, 2018

This code contains functions for analyzing photon arrival times using BINNING. 

For example usage, see if __name__ == "__main__": 
"""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib
from scipy.optimize import curve_fit

from mkidpipeline.speckle.genphotonlist_IcIsIr import genphotonlist
from mpmath import mp, hyp1f1
import mpmath
from scipy import special
from scipy.special import eval_laguerre, eval_genlaguerre, factorial
from scipy import optimize
from mpl_toolkits.axes_grid1 import make_axes_locatable
from scipy.stats import poisson

import time


def blurredMR(n,Ic,Is):
    """
    Calculates the probability of getting a bin with n counts given Ic & Is. 
    n, Ic, Is must have the same units. 
    
    INPUTS:
        n - array of the number of counts you want to know the probability of encountering. numpy array, can have length = 1. Units are the same as Ic & Is
        Ic - the constant part of the speckle pattern [counts/time]. User needs to keep track of the bin size. 
        Is - the random part of the speckle pattern [units] - same as Ic
    OUTPUTS:
        p - array of probabilities
        
    EXAMPLE:
        n = np.arange(8)
        Ic,Is = 4.,6.
        p = blurredMR(n,Ic,Is)
        plt.plot(n,p)
        #plot the probability distribution of the blurredMR vs n
        
        n = 5
        p = blurredMR(n,Ic,Is)
        #returns the probability of getting a bin with n counts
    
    """
    
    n = n.astype(int)
    p = np.zeros(len(n))
    for ii in range(len(n)):
        p[ii] = 1/(Is + 1)*(1 + 1/Is)**(-n[ii])*np.exp(-Ic/Is)*hyp1f1(float(n[ii]) + 1,1,Ic/(Is**2 + Is))
    return p



def modifiedRician(I, Ic, Is):
    '''
    MR pdf(I) = 1/Is * exp(-(I+Ic)/Is) * I0(2*sqrt(I*Ic)/Is)
    mean = Ic + Is
    variance = Is^2 + 2*Ic*Is
    '''
    mr = 1.0/Is * np.exp(-1.0*(I+Ic)/Is)* special.iv(0,2.0*np.sqrt(I*Ic)/Is)
    return mr



def getLightCurve(photonTimeStamps,startTime =0,stopTime =10,effExpTime=.01):
    """
    Takes a 1d array of arrival times and bins it up with the given effective exposure 
    time to make a light curve.
    
    INPUTS:
        photonTimeStamps - 1d numpy array with units of microseconds
    OUTPUTS:
        lightCurveIntensityCounts - array with units of counts. Float.
        lightCurveIntensity - array with units of counts/sec. Float.
        lightCurveTimes - array with times corresponding to the bin centers of the light curve. Float.
    """
    histBinEdges = np.arange(startTime,stopTime,effExpTime)
    
    hist,_ = np.histogram(photonTimeStamps/10**6,bins=histBinEdges) #if histBinEdges has N elements, hist has N-1
    lightCurveIntensityCounts = hist  #units are photon counts
    lightCurveIntensity = 1.*hist/effExpTime  #units are counts/sec
    lightCurveTimes = histBinEdges[:-1] + 1.0*effExpTime/2
    
    return lightCurveIntensityCounts, lightCurveIntensity, lightCurveTimes
    # [lightCurveIntensityCounts] = counts
    # [lightCurveIntensity] = counts/sec
    
    
    
    

def histogramLC(lightCurve):
    """
    makes a histogram of the light curve intensities
    
    INPUTS:
        lightCurve - 1d array specifying number of photons in each bin
    OUTPUTS:
        intensityHist - 1d array containing the histogram
        bins - 1d array specifying the bins (0 photon, 1 photon, etc.)
    
    
    """
    #Nbins=30  #smallest number of bins to show
    
    Nbins = int(np.amax(lightCurve))
    
    if Nbins==0:
        intensityHist = np.zeros(30)
        bins = np.arange(30)
        #print('LightCurve was zero for entire time-series.')
        return intensityHist, bins
    
    #count the number of times each count rate occurs in the timestream
    intensityHist, _ = np.histogram(lightCurve,bins=Nbins,range=[0,Nbins])
    
    intensityHist = intensityHist/float(len(lightCurve))      
    bins = np.arange(Nbins)
    
    return intensityHist, bins



def muVar_to_IcIs(mu,var,effExpTime):
    """
    given a mean count rate mu and variance of the count rate of a light curve,
    calculate Ic and Is.
    INPUTS:
        mu - the mean count rate. [counts/bin]
        var - the variance of the count rate [counts/bin]
    OUTPUTs:
        Ic - counts/sec
        Is - counts/sec
    """
    
    if not np.isnan(np.sqrt(mu**2 - var + mu)):
        Ic = np.sqrt(mu**2 - var + mu)
        Is = mu - Ic
    else:
        return

        
    Ic/=effExpTime
    Is/=effExpTime    
    
    return Ic,Is



def fitBlurredMR(bins,intensityHist,effExpTime, **kwargs): 
    """
    fit a blurred modified rician to a histogram of intensities using curve_fit
    INPUTS:
        bins - 1d array specifying the bins (0 photons, 1 photon, etc.)
        intensityHist - 1d array containing the histogram
        effExpTime - effective exposure time, aka bin size. [seconds]
        **kwargs - keywords for fitBlurredMR, including
            Ic_guess
            Is_guess
    OUTPUTS:
        Ic - counts/sec
        Is - counts/sec
        pcov - covariance matrix returned by curve_fit
    """
    try: Ic_guess = kwargs['Ic_guess']
    except: p0 = [1,1]
    else:
        Is_guess = kwargs['Is_guess']
        p0 = [Ic_guess, Is_guess]
    
    sigma = np.sqrt(intensityHist)
    sigma[np.where(sigma==0)] = 1
#    print('\np0 is ',p0)
#    print('Ic_guess, Is_guess are: ',Ic_guess,Is_guess,'\n\n')
    try:
        popt,pcov = curve_fit(blurredMR,bins,intensityHist,p0=p0,sigma=sigma,bounds=(0,np.inf))
        #units of pcov are I^2
        
        Ic = popt[0]
        Is = popt[1]
    except RuntimeError:
        Ic, Is = 1,0.1
        print('WARNING: curve_fit failed :(')
    else:
        Ic/=effExpTime
        Is/=effExpTime
    return Ic,Is,pcov



def binMRlogL(n, Ic, Is):
    '''
    Given a light curve, calculate the Log likelihood that
    its intensity distribution follows a blurred modified Rician with Ic, Is. 
    
    INPUTS:
        n: 1d array containing the (binned) intensity as a function of time, i.e. a lightcurve [counts/bin]. Bin size must be fixed. 
        Ic: Coherent portion of MR [cts/bin]
        Is: Speckle portion of MR [cts/bin]
    OUTPUTS:
        [float] the Log likelihood. 
    '''
    if Ic<=0 or Is<=0:
        print('Ic or Is are <= zero. Ic, Is = {:g}, {:g}'.format(Ic,Is))
        lnL = -np.inf
        return lnL
    
    N = len(n)
    lnL = np.zeros(N) 
    tmp = np.zeros(N)
    
    if 1./(Is+1)<=0 or Is/(1.+Is) <=0:
        lnL = np.nan
        return lnL
    
    #hyp1f1 can't do numpy arrays because of its data type, which is mpf

# v1
##    for ii in range(len(n)): 
##        tmp[ii] = float(hyp1f1(n[ii] + 1,1,Ic/(Is**2 + Is)))
    
#    tmp = np.array([float(hyp1f1(i + 1,1,Ic/(Is**2 + Is))) for i in n])
#    lnL = np.log(1./(Is+1)) - n*np.log(1+1./Is) - Ic/Is + np.log(tmp)
#    
#    return np.sum(lnL)

# v2, gains about a factor of 10 in speed over v1
#    k = Ic/(Is**2 + Is)
#    tmp = np.log([eval_laguerre(i,-k)*np.exp(k) for i in n])
#    lnL = N*(np.log(1./(Is+1))  - 1.*Ic/Is) + np.sum(tmp) + np.sum(n)*np.log(Is/(1.+Is))
#    
#    return lnL
    
    
# v3, another factor of ~5 in speed over v2
    #Use scipy.special.eval_laguerre instead of the hypergeometric function because
    #it's WAY faster. 
#    k = Ic/(Is**2 + Is)
#    tmp = np.log([eval_laguerre(i,-k) for i in n])
#    tmp += k
#    lnL = N*(np.log(1./(Is+1))  - 1.*Ic/Is) + np.sum(tmp) + np.sum(n)*np.log(Is/(1.+Is))
#    
#    return lnL


# v4. eval_laguerre can accept numpy arrays! Another factor of ~9 in speed. 
    k = -Ic/(Is**2 + Is)
    tmp = np.log(eval_laguerre(n,k))
    tmp -= k
    lnL = N*(np.log(1./(Is+1))  - Ic/Is) + np.sum(tmp) + np.sum(n)*np.log(Is/(1.+Is))
#    junk = (np.log(1./(Is+1))  - Ic/Is) + tmp + n*np.log(Is/(1.+Is))
    
    return lnL


def binMR_like(n, Ic, Is):
    '''
    Given a light curve, calculate the likelihood that
    its intensity distribution follows a blurred modified Rician with Ic, Is. 
    
    INPUTS:
        n: 1d array containing the (binned) intensity as a function of time, i.e. a lightcurve [counts/bin]. Bin size must be fixed. 
        Ic: Coherent portion of MR [cts/bin]
        Is: Speckle portion of MR [cts/bin]
    OUTPUTS:
         the likelihood. 
    '''
#    k = -Ic/(Is**2 + Is)
#    like = (1+1/Is)**-n/(1+Is)*np.exp(-Ic/Is-k)*eval_laguerre(n,k)
#    like[np.argwhere(np.isnan(like))]=0
#    return like
    
#    like = np.zeros(len(n))
#    for ii in range(len(n)):
#        like[ii] = np.exp(binMRlogL(n[ii:ii+1],Ic,Is))
    
    if Ic<=0 or Is<=0:
        print('Ic or Is are <= zero. Ic, Is = {:g}, {:g}'.format(Ic,Is))
        like = 0.
        likeArray = np.zeros(len(n))
        return like
    
    k = -Ic/(Is**2 + Is)
    tmp = np.log(eval_laguerre(n,k))
    tmp -= k
    likeArray = np.exp(np.log(1./(Is+1))  - 1.*Ic/Is + tmp + n*np.log(Is/(1.+Is)))
    like = 1.
    tmp = mp.mpf(1)*likeArray
    for ii in tmp:
        like*=ii
     
    if like==0:
        print('k = ', k)
        print('likeArray = ', likeArray)
        print('tmp = ', tmp)
        
    return like, likeArray


def pssn(m,mu): #scipy has a module called poisson
    return np.exp(-mu)*mu**m/factorial(m)


def loglike_planet_blurredMR(n,Ic,Is,Ir):
    """
    Calculate the log likelihood of lightcurve that has both speckle Ic and Is,
    as well as planet light Ir.
    
    This might break if you give it values of Ic Is Ir that are too big. mpmath
    might give complex answers when calling the mpmath.log(tiny number)
    """
#    if Ir>3:
#        maxx = int(5*Ir)
#        tmp = np.arange(maxx)
#        cpd = poisson.cdf(tmp,Ir)
#        mm = tmp[np.where(cpd<.99999)]
#
#    else:
#        maxx = 15
#        tmp = np.arange(maxx)
#        cpd = poisson.cdf(tmp,Ir)
#        mm = tmp[np.where(cpd<.99999)]
    
    like = np.zeros(len(n))
    


#    t1 = time.time()
#    for ii in range(len(n)):
#        for mm in range(int(n[ii])+1):  #make sure it executes at least 1 time
#            like[ii] += pssn(mm,Ir)*binMR_like(np.array([n[ii]-mm]), Ic, Is)[0]
#    
#    t2 = time.time()
#    
#    logL=0
#    for jj in [mp.log(ii) for ii in like]:
#        logL +=jj
#        
#    t3 = time.time()
    
            
        
        
        
    #make a lookup table
    lutSize = int(max(n))+1
    lut = np.zeros(lutSize)
    loglut = np.zeros(lutSize)
    inds = np.unique(n).astype(int)
    for ii in inds:
        for mm in np.arange(ii+1):
            lut[ii] += pssn(mm,Ir)*binMR_like(np.array([ii-mm]), Ic, Is)[0]    
    loglut = np.log(lut)
    like2 = lut[n]
#    print('\n\nlut = ',lut,'\n\n')
    
#    logL2=0
#    for jj in [mpmath.log(ii) for ii in like2]:
#        logL2 +=jj
#    logL2 = np.float(logL2)
        
    logL3 = np.sum(loglut[n])
    
#    t4 = time.time()
#    print('foo = ', t2-t1)
#    print('foo = ', t3-t2)
#    print('foo = ', t4-t3)

#    logL = np.sum(np.log(like))
#    plt.legend()
    
#    print('\n\ntmp: ',tmp)
#    print('cpd: ',cpd)
#    print('mm: ',mm)

#    return logL, like, logL2, like2
    return logL3, like2


def negloglike_planet_blurredMR(p,n):
    return -loglike_planet_blurredMR(n, p[0], p[1], p[2])[0]
    
    
    


def negLogLike(p,n):
    """
    Wrapper for getting the negative log likelihood of the binned 
    & blurred modified rician. The inputs are given in a 
    different order because that's what scipy.optimize.minimize wants.
    
    Use this with scipy.optimize.minimize
    
    INPUTS:
        p: a 2 element numpy array, where the first element is Ic and the second element is Is. 
        n: 1d array containing the (binned) intensity as a function of time, i.e. a lightcurve [counts/sec]. Bin size must be fixed. 
    OUTPUTS:
        negative log likelihood [float]
    """
    return -binMRlogL(n, p[0], p[1])


def nLogLikeJac(p,n):
    
    return -binMRlogL_jacobian(n,p[0], p[1])
    
    
def nLogLikeHess(p,n):
        
    return -binMRlogL_hessian(n,p[0], p[1])


def maxBinMRlogL(n, Ic_guess=1., Is_guess=1., method='Newton-CG'):  # Newton-CG
    """
    Find the maximum likelihood values for Ic and Is for a given lightcurve.
    
    INPUTS:
        n: 1d array containing the (binned) intensity as a function of time, i.e. a lightcurve [counts/sec]. Bin size must be fixed. 
        Ic_guess: initial guess for Ic for optimization routine
        Is_guess: initial guess for Is for optimization routine
    OUTPUTS:
        Ic: max likelihood estimate for Ic
        Is: max likelihood estimate for Is
    """
    p0 = np.array([Ic_guess,Is_guess])
    
#    t1 = time.time()
    
#    res = optimize.minimize(negLogLike, p0,n,bounds=((0.1,np.inf),(0.1,np.inf)))
    
    res = optimize.minimize(negLogLike, p0,n, method=method,jac = nLogLikeJac, hess=nLogLikeHess )

#    t2 = time.time()
#    dT = t2 - t1
#    print('\nelapsed time for estimating Ic & Is by finding the maximum likelihood: ', dT, 'sec\n')
    
    Ic = res.x[0]/effExpTime
    Is = res.x[1]/effExpTime
    
#    print('\nIc,Is = ', Ic, Is)
    
    return Ic,Is,res



def binMRlogL_jacobian(n,Ic,Is):
    """
    Finds the Jacobian of the log likelihood function at the specified Ic and 
    Is for a given lightcurve n.
    The Jacobian is a vector of the first derivatives. 
    
    INPUTS:
        n: 1d array containing the (binned) intensity as a function of time, i.e. a lightcurve [counts/bin]. Bin size must be fixed. 
        Ic: counts/bin
        Is: counts/bin
    OUTPUTS:
        jacobian vector [dlnL/dIc, dlnL/dIs] at Ic, Is
    """
    N = len(n)
    k = -Ic/(Is**2 + Is)
    tmp1 = eval_genlaguerre(n-1,1,k)
    tmp2 = eval_laguerre(n,k)
    tmp4 = tmp2*Is*(1+Is)
    jac_Ic = -N/(1+Is) + np.sum(tmp1/tmp4)
    
    tmp3 = 1/(Is + Is**2)
    jac_Is = N*(-1+Ic-Is)/(1+Is)**2 + np.sum(tmp3*n + k*tmp3*(1+2*Is)*tmp1/tmp2 ) 
    
#   check that my simplifications are correct. Compare the results to the raw output 
#   from mathematica. 
#    x = -N/Is + N/(Is + Is**2) + np.sum(eval_genlaguerre(n-1,1,k)/((Is + Is**2)*eval_laguerre(n,k)))
#    y = N*(-1 + Ic - Is)/(1 + Is)**2 + np.sum( (1+Is)*(-Is/(1 + Is)**2 + 1/(1+Is) )/Is*n - 
#           Ic*(1 + 2*Is)/(Is + Is**2)**2*eval_genlaguerre(n-1,1,k)/eval_laguerre(n,k))

    return np.asarray([jac_Ic, jac_Is])
    
    
    
def binMRlogL_hessian(n,Ic,Is):
    """
    Finds the Hessian of the log likelihood function at the specified Ic and 
    Is for a given lightcurve n.
    The Hessian is a matrix of the second derivatives. 
    
    INPUTS:
        n: 1d array containing the (binned) intensity as a function of time, i.e. a lightcurve [counts/bin]. Bin size must be fixed. 
        Ic: counts/bin
        Is: counts/bin
    OUTPUTS:
        Hessian matrix [[d2lnL/dIc2, d2lnL/dIcdIs], [d2lnL/dIsdIc, d2lnL/dIsdIs]] at Ic, Is
    """
    
    N = len(n)
    tmp1 = 1/(Is + Is**2)
    tmp2 = 1/(1+Is)
    k = -Ic*tmp1

#   check that my simplifications are correct. Compare the results to the raw output
#   from mathematica.
#    H_IcIc = np.sum(eval_genlaguerre(n-2,2,k)/eval_laguerre(n,k) - eval_genlaguerre(n-1,1,k)**2/eval_laguerre(n,k)**2)*tmp1**2
#    H_IcIs = N/(1+Is)**2 + np.sum( - Ic*(1+2*Is)*eval_genlaguerre(n-2,2,k)*tmp1*tmp1*tmp1/eval_laguerre(n,k) - (1+2*Is)*eval_genlaguerre(n-1,1,k)*tmp1*tmp1/eval_laguerre(n,k) + Ic*(1+2*Is)*eval_genlaguerre(n-1,1,k)**2*tmp1*tmp1*tmp1/eval_laguerre(n,k)**2 )
#    H_IsIs = N*(1-2*Ic+Is)*tmp2**3 + np.sum( -(1+2*Is)*n/(Is**2)*tmp2**2 + Ic**2*(1+2*Is)**2*eval_genlaguerre(n-2,2,k)*tmp1**4/eval_laguerre(n,k) + 2*Ic*(1+2*Is)**2*eval_genlaguerre(n-1,1,k)*tmp1**3/eval_laguerre(n,k) -  2*Ic*eval_genlaguerre(n-1,1,k)*tmp1**2/eval_laguerre(n,k) - Ic**2*(1+2*Is)**2*eval_genlaguerre(n-1,1,k)**2*tmp1**4/eval_laguerre(n,k)**2 )
#    Hessian = np.asarray([[H_IcIc, H_IcIs],[H_IcIs, H_IsIs]])

    N = len(n)
    a = 1/(1+Is)                  # a = 1/(1 + Is)
    b = a/Is                      # b = 1/(Is + Is**2)
    c = 1 + 2*Is                  # c = 1 + 2*Is
    d = b*b                       # d = 1/(Is+ Is**2)**2
    e = d*b                       # e = 1/(Is+ Is**2)**3
    f = e*b                       # f = 1/(Is+ Is**2)**4
    g = Ic*c                      # g = Ic*(1 + 2*Is)
    h = Ic**2                     # h = Ic**2
    
    k = -Ic*b                     # k = -Ic/(Is + Is**2)   last argument of every laguerre call
    l = 1/eval_laguerre(n,k)      # l = 1/eval_laguerre(n,k)
    m = l*l                       # m = 1/eval_laguerre(n,k)**2
    q = eval_genlaguerre(n-1,1,k) # q = eval_genlaguerre(n-1,1,k)
    o = q*q                       # o = eval_genlaguerre(n-1,1,k)**2
    p = eval_genlaguerre(n-2,2,k) # p = eval_genlaguerre(n-2,2,k)

    
    h_IcIc = np.sum(p*d*l - o*d*m)
    
    h_IcIs = N*a**2 + np.sum( -g*p*e*l - c*q*d*l + g*o*e*m )
    
    h_IsIs = N*(1-2*Ic+Is)*a**3 + np.sum( -c*n/Is**2*a**2  + h*c**2*p*f*l + 2*Ic*c**2*q*e*l - 2*Ic*q*d*l - h*c**2*o*f*m  )
    
    hessian = np.asarray([[h_IcIc, h_IcIs],[h_IcIs, h_IsIs]])
    
    

    return hessian




def logLMap(n, Ic_list, Is_list, effExpTime,IcPlusIs = False):
    """
    makes a map of the MR log likelihood function over the range of Ic, Is

    INPUTS:
        n - light curve [counts]
        Ic_list - list of Ic values [photons/second]
        Is_list - list
    OUTPUTS:
        X - meshgrid of x coords
        Y - meshgrid of y coords
        im - log likelihood map
    """
    Ic_list_countsperbin = Ic_list * effExpTime  # convert from cps to counts/bin
    Is_list_countsperbin = Is_list * effExpTime

    im = np.zeros((len(Is_list), len(Ic_list)))

    for j, Is in enumerate(Is_list_countsperbin):
        for i, Ic in enumerate(Ic_list_countsperbin):
            if IcPlusIs == True:
                tmp = Ic - Is
            else:
                tmp = Ic

#            lnL = binMRlogL(n, tmp, Is)
            lnL = loglike_planet_blurredMR(n,tmp,Is,.8)[0]
            im[j,i] = lnL
        print('Ic,Is = ', Ic / effExpTime, Is / effExpTime)

    Ic_ind, Is_ind = np.unravel_index(im.argmax(), im.shape)
    print('Max at (' + str(Ic_ind) + ', ' + str(Is_ind) + ')')
    print("Ic=" + str(Ic_list[Ic_ind]) + ", Is=" + str(Is_list[Is_ind]))
    print(im[Ic_ind, Is_ind])

    X, Y = np.meshgrid(Ic_list, Is_list)
    sigmaLevels = np.array([8.36, 4.78, 2.1])
    levels = np.amax(im) - sigmaLevels

    return X,Y,im




def plotLogLMap(n, Ic_list, Is_list, effExpTime,IcPlusIs = False):
    """
    plots a map of the MR log likelihood function over the range of Ic, Is
    
    INPUTS:
        n - light curve [counts]
        Ic_list - list of Ic values [photons/second]
        Is_list - list
    OUTPUTS:
        
    """
    
    
    Ic_list_countsperbin = Ic_list*effExpTime  #convert from cps to counts/bin
    Is_list_countsperbin = Is_list*effExpTime
    
    im = np.zeros((len(Ic_list),len(Is_list)))
            
    for j, Is in enumerate(Is_list_countsperbin):
        for i, Ic in enumerate(Ic_list_countsperbin):
            if IcPlusIs==True:
                tmp = Ic - Is
            else:
                tmp = Ic
                
            lnL = binMRlogL(n, tmp, Is)
            im[i,j] = lnL
        print('Ic,Is = ',Ic/effExpTime,Is/effExpTime)
       
    Ic_ind, Is_ind=np.unravel_index(im.argmax(), im.shape)
    print('Max at ('+str(Ic_ind)+', '+str(Is_ind)+')')
    print("Ic="+str(Ic_list[Ic_ind])+", Is="+str(Is_list[Is_ind]))
    print(im[Ic_ind, Is_ind])



    X, Y = np.meshgrid(Ic_list, Is_list)
    sigmaLevels = np.array([8.36, 4.78, 2.1])
    levels = np.amax(im) - sigmaLevels

    MYSTYLE = {'contour.negative_linestyle':'solid'}
    oldstyle = {key:matplotlib.rcParams[key] for key in MYSTYLE}
    matplotlib.rcParams.update(MYSTYLE)

    tmpim = im.T - np.amax(im)
    fig, ax = plt.subplots()
    img = ax.imshow(tmpim, extent=[np.amin(Ic_list), np.amax(Ic_list), np.amin(Is_list), np.amax(Is_list)], aspect='auto', origin='lower', cmap='hot_r', vmin=-8, vmax=0, interpolation='spline16')

    CS = ax.contour(X, Y, im.T, colors='black', levels=levels)

    fmt = {}
    strs = [r'3$\sigma$', r'2$\sigma$', r'1$\sigma$']
    for l, s in zip(CS.levels, strs):
        fmt[l] = s
    plt.clabel(CS, inline=True, fmt=fmt, fontsize=8)

    plt.plot(Ic_list[Ic_ind], Is_list[Is_ind], "xr")
    if IcPlusIs == True:
        plt.xlabel('Ic + Is [/s]')
    else:
        plt.xlabel('Ic [/s]')
    plt.ylabel('Is [/s]')
    plt.title('Map of log likelihood. Bin size = {:g}s'.format(effExpTime))





    divider = make_axes_locatable(ax)
    cax = divider.append_axes('right', size = '5%', pad=0.4)
    cbar = fig.colorbar(img,cax=cax)
    cbar.set_label(r'ln$\mathcal{L}$ - ln$\mathcal{L}_{max}$')



    # fig, ax = plt.subplots()
    # X, Y = np.meshgrid(Ic_list, Is_list)
    # sigmaLevels = np.array([8.36, 4.78, 2.1])
    # levels = np.amax(im) - sigmaLevels
    #
    # MYSTYLE = {'contour.negative_linestyle':'solid'}
    # oldstyle = {key:matplotlib.rcParams[key] for key in MYSTYLE}
    # matplotlib.rcParams.update(MYSTYLE)
    #
    # tmpim = im.T - np.amax(im)
    # cax = ax.imshow(tmpim,extent = [np.amin(Ic_list), np.amax(Ic_list), np.amin(Is_list), np.amax(Is_list)],aspect='auto',origin = 'lower', cmap = 'hot_r',vmin=-8, vmax=0, interpolation='spline16')
    # cbar = fig.colorbar(cax, orientation = 'horizontal')
    # cbar.set_label(r'ln$\mathcal{L}$ - ln$\mathcal{L}_{max}$')



    

    
    matplotlib.rcParams.update(oldstyle)

    plt.show()

    return X,Y,im






if __name__ == "__main__":

    if 0:
        print("Generating photon list...",end="", flush=True)
#        Ic, Is, Ttot, tau = [300., 30., 300., .1] # [Ic, Is] = cps, [Ttot, tau] = sec
#        ts = genphotonlist(Ic, Is, Ttot, tau)
        
        Ic, Is, Ir, Ttot, tau = [300., 30., 80, 300., .1] # [Ic, Is] = cps, [Ttot, tau] = sec
        ts = genphotonlist(Ic, Is, Ir, Ttot, tau)
        
        print("\nPhoton list parameters:\n Ic, Is, Ir, Ttot, tau = [{:g}, {:g}, {:g}, {:g}, {:g}]".format(Ic, Is, Ir, Ttot, tau))
        
        print("[Done]\n")
    
        print("=====================================")
    
    
    if 0:
        """
        Make a plot showing a histogram fit.
        """
        effExpTime = 0.01 #second

        
        lightCurveIntensityCounts, lightCurveIntensity, lightCurveTimes = getLightCurve(ts,ts[0]/1e6,ts[-1]/1e6,effExpTime)
        if 0:
            plt.figure(1)
            plt.plot(lightCurveTimes,lightCurveIntensityCounts)
            plt.xlabel('time [s]')
            plt.ylabel('counts')
            plt.title('light curve')
            plt.show()
        
        intensityHist, bins = histogramLC(lightCurveIntensityCounts)
        plt.figure(2)
        plt.bar(bins,intensityHist)
        plt.xlabel('intensity [counts/bin]')
        plt.ylabel('frequency')
        plt.title('intensity histogram. Effective exposure time = {:.4f}'.format(effExpTime))
        
        
        Ic_est,Is_est,covMatrix = fitBlurredMR(bins,intensityHist,effExpTime)
        
        print('\nIc and Is from curve_fit are: ',Ic_est,'  ',Is_est, ' counts/sec')
        
        #calculate the error on Ic and Is using the covariance matrix returned by curve_fit
        perr = np.sqrt(np.diag(covMatrix))/effExpTime #1 sigma errors, assuming the likelihood function is gaussian.
        
        
        mu = np.mean(lightCurveIntensityCounts)
        var = np.var(lightCurveIntensityCounts)  
    

        plt.plot(bins,blurredMR(np.arange(len(bins)),Ic_est*effExpTime,Is_est*effExpTime),'.-k',label = r'blurred MR from curve_fit. Ic,Is = {:.2f} $\pm$ {:.2f}, {:.2f} $\pm$ {:.2f}'.format(Ic_est,perr[0],Is_est,perr[1]))
        plt.plot(bins,blurredMR(np.arange(len(bins)),Ic*effExpTime,Is*effExpTime),'.-b',label = 'blurred MR from actual Ic and Is. Ic,Is = {:.2f}, {:.2f}'.format(Ic,Is))
#        plt.plot(bins,modifiedRician(np.arange(len(bins)),Ic_est*effExpTime,Is_est*effExpTime),'.-r',label = 'pure MR  Ic,Is = {:.2f}, {:.2f}'.format(Ic,Is))
        
        
        
        ''''
        try:
            IIc = np.sqrt(mu**2 - var + mu)  #intensity per bin. 
        except:
            pass
        else:
            IIs = mu - IIc  #intensity per bin
            
            IIc/=effExpTime  #change to intensity per second
            IIs/=effExpTime  #change to intensity per second
            
            plt.plot(bins,blurredMR(np.arange(len(bins)),IIc*effExpTime,IIs*effExpTime),'.-c',label = 'blurredMR from mean and variance Ic,Is = {:.2f}, {:.2f}'.format(IIc,IIs))
            
        '''
        
        try:
            IIc,IIs = muVar_to_IcIs(mu,var,effExpTime)
        except:
            print('unable to get Ic and Is from mean and variance.')
        else:
            plt.plot(bins,blurredMR(np.arange(len(bins)),IIc*effExpTime,IIs*effExpTime),'.-c',label = 'blurredMR from mean and variance Ic,Is = {:.2f}, {:.2f}'.format(IIc,IIs))
            
    
        plt.legend()
        
        
        
    if 0:
        """
        Plot Ic,Is vs effExpTime
        """
    
        effExpTimeArray = np.arange(.05,.2,.05) #second   (.005,.5,.005)
        
        IcArray = np.array([])
        IsArray = np.array([])
        covMatrixArray = np.array([])
        IIcArray = np.array([])
        IIsArray = np.array([])
        NdownSampleArray = np.array([])
        
        for effExpTime in effExpTimeArray:
            lightCurveIntensityCounts, lightCurveIntensity, lightCurveTimes = getLightCurve(ts,ts[0]/1e6,ts[-1]/1e6,effExpTime)
            

            if 0:
                '''
                Down-sample the light curve so that we do a fit of the distribution after removing
                some bins. Use the decorrelation time to determine how often to select a bin, then
                discard the bins in between for that particular fit of Ic & Is. 
                
                For example, if the decorrelation time is 8*effExpTime, do the fit
                with bins 0,8,16,24,... and ignore bins 1-7, 9-15, 17-23, ...
                Then do a fit with bins 1,9,17,25 while ignoring bins 2-8, 10-16, ...
                '''
                NdownSample = 1*np.ceil(tau/effExpTime).astype(int) 
                NdownSampleArray = np.append(NdownSampleArray,NdownSample)
                lightCurveIntensityCounts = lightCurveIntensityCounts[::NdownSample]
                lightCurveIntensity = lightCurveIntensity[::NdownSample]
                lightCurveTimes = lightCurveTimes[::NdownSample]
            
            intensityHist, bins = histogramLC(lightCurveIntensityCounts)
            
            
            mu = np.mean(lightCurveIntensityCounts)
            var = np.var(lightCurveIntensityCounts)
            
            '''
            if not np.isnan(np.sqrt(mu**2 - var + mu)):
                IIc = np.sqrt(mu**2 - var + mu)
                IIs = mu - IIc
            else:
                IIc = mu/2
                IIs = mu - IIc
            '''
            
            
            try:
                IIc,IIs = muVar_to_IcIs(mu,var,effExpTime)
            except:
                IIc = mu/2       #just create a reasonable seed for curve_fit
                IIs = mu - IIc
                IIc/=effExpTime
                IIs/=effExpTime
                
                
                
            IIcArray= np.append(IIcArray,IIc)
            IIsArray= np.append(IIsArray,IIs)
            '''
            old code:
            try:
                IIc = np.sqrt(mu**2 - var + mu)
            except:
                IIc = 0
            else:
                IIs = mu - IIc
                IIcArray= np.append(IIcArray,IIc/effExpTime)
                IIsArray= np.append(IIsArray,IIs/effExpTime)
            
            '''

            Ic_est,Is_est,covMatrix = fitBlurredMR(bins,intensityHist,effExpTime,Ic_guess = IIc,Is_guess = IIs)
            
            IcArray = np.append(IcArray,Ic_est)
            IsArray = np.append(IsArray,Is_est)
            covMatrixArray = np.append(covMatrixArray,covMatrix)
            


  
        covMatrixArray = covMatrixArray.reshape(len(effExpTimeArray),2,2)
        #calculate the error on Ic and Is using the covariance matrix returned by curve_fit
        Icerr = np.sqrt(covMatrixArray[:,0,0])/effExpTimeArray #1 sigma errors, assuming the likelihood function is gaussian.
        Iserr = np.sqrt(covMatrixArray[:,1,1])/effExpTimeArray
        
        if len(np.where(Icerr>2*IcArray)[0])>0 or len(np.where(Iserr>2*IsArray)[0])>0:
            print('\n\nSome points have HUGE error bars. Setting the error bars to zero so the plot is still usable. ')
            temp = np.where(Icerr>2*IcArray)[0]
            print('\nThe indices in Icerr with values greater than 2*Ic are: \n',temp)
            Icerr[temp]=0  #sometimes the error bars are huge and screw up the plot.
            temp = np.where(Iserr>2*IsArray)[0]
            print('\nThe indices in Iserr with values greater than 2*Is are: \n',temp)
            Iserr[temp]=0  #Just set the error to zero, so it's obvious something is wrong.
        

        
        
        plt.figure(3)
        plt.plot(effExpTimeArray,IcArray,'b.-',label = 'fit from binned light curve')
        plt.plot(effExpTimeArray,IIcArray,'k.-',label = 'from mean and variance')
        plt.plot(effExpTimeArray,Ic*np.ones(len(effExpTimeArray)),'r')
        plt.fill_between(effExpTimeArray,IcArray+Icerr,IcArray-Icerr,alpha = 0.4)
        plt.axvline(x=tau,color='g',ls='--')
        plt.xlabel('effective exposure time [sec]')
        plt.ylabel('counts/sec')
        plt.title('fitted Ic')
        plt.legend()
        
        plt.figure(4)
        plt.plot(effExpTimeArray,IsArray,'b.-',label = 'fit from binned light curve')
        plt.plot(effExpTimeArray,IIsArray,'k.-',label = 'from mean and variance')
        plt.plot(effExpTimeArray,Is*np.ones(len(effExpTimeArray)),'r')
        plt.fill_between(effExpTimeArray,IsArray+Iserr,IsArray-Iserr,alpha = 0.4)
        plt.axvline(x=tau,color='g',ls='--')
        plt.xlabel('effective exposure time [sec]')
        plt.ylabel('counts/sec')
        plt.title('fitted Is')
        plt.legend()
        
        
        '''
        Plot the down-sampling parameter
        
        plt.figure(5)
        plt.plot(effExpTimeArray,NdownSampleArray-1,'b.-')
        plt.axvline(x=tau,color='g',ls='--')
        plt.xlabel('effective exposure time [sec]')
        plt.ylabel('# of bins discarded per effExpTime')
        plt.title('down-sample parameter')        
        '''
        
        
        
    if 0:
        '''
        Plot the log likelihood vs Is while keeping Ic constant. 
        '''
        effExpTime = 0.01 #second
        lightCurveIntensityCounts, lightCurveIntensity, lightCurveTimes = getLightCurve(ts,ts[0]/1e6,ts[-1]/1e6,effExpTime)
        
        Ic = 100.
        Is = np.linspace(30,300,11)
        logLArray = np.zeros(len(Is))
        for ii in range(len(Is)):    
            logLArray[ii] = binMRlogL(lightCurveIntensityCounts,Ic,Is[ii])
            
        plt.plot(Is,logLArray,'b.-')
        plt.xlabel('Is')
        plt.ylabel('log L')
        plt.title('Ic = {}'.format(Ic))
        
        
        
    if 0:
        x = np.arange(30)
        Ic = 3.
        Is = .3
        y = blurredMR(x,Ic,Is)
        plt.plot(x,y,'b.-')
        
        tmp = np.amax(y)
        x = np.arange(300)
        Ic = 30.
        Is = 3.
        y = blurredMR(x,Ic,Is)
        plt.plot(x/10,y/np.amax(y)*tmp,'r.-')
        print('sum is: ', np.sum(y))
            
            
        
    if 0:
        
        effExpTime = .01
        lightCurveIntensityCounts, lightCurveIntensity, lightCurveTimes = getLightCurve(ts,ts[0]/1e6,ts[-1]/1e6,effExpTime)
        
        
        print("Mapping...")
        Ic_list=np.linspace(285,315,25)  #linspace(start,stop,number of steps)
        Is_list=np.linspace(25,35,25)
        X,Y,im = plotLogLMap(lightCurveIntensityCounts, Ic_list, Is_list, effExpTime)
        
        """
        Save the logL plot data in a pickle file:
        
        with open('junk.pkl','wb') as f:
            pickle.dump([ts,Ic_list,Is_list,X,Y,im],f)
            f.close()
            
            
        
        Open the LogL plot data in another python session:
            
        with open('junk.pkl','rb') as f:
            ts,Ic_list,Is_list,X,Y,im = pickle.load(f)
            f.close() 
            
        plt.contourf(X,Y,im.T)
        plt.xlabel('Ic [/s]')
        plt.ylabel('Is [/s]')
        plt.title('Map of log likelihood')
        plt.show()
            
            
        """
        
        
        
    if 0:
        effExpTime = .01
        lightCurveIntensityCounts, lightCurveIntensity, lightCurveTimes = getLightCurve(ts,ts[0]/1e6,ts[-1]/1e6,effExpTime)
        
        print("Calling scipy.optimize.minimize to find Ic,Is...")
        
        mu = np.mean(lightCurveIntensityCounts)
        var = np.var(lightCurveIntensityCounts)

        try:
            IIc,IIs = np.asarray(muVar_to_IcIs(mu,var,effExpTime))*effExpTime
        except:
            print('\nmuVar_to_IcIs failed\n')
            IIc = mu/2       #just create a reasonable seed 
            IIs = mu - IIc
#            IIc/=effExpTime
#            IIs/=effExpTime
        
        Ic,Is,res = maxBinMRlogL(lightCurveIntensityCounts, Ic_guess=IIc, Is_guess=IIs)
        
        print('\nIc,Is = ', Ic, Is)
        print("[Done]\n")
        
        print("=====================================")
        
        
        
    if 0:
        """
        Make a map of log likelihood with Is and Ic+Is for the axes. 
        """
        
        effExpTime = .001
        lightCurveIntensityCounts, lightCurveIntensity, lightCurveTimes = getLightCurve(ts,ts[0]/1e6,ts[-1]/1e6,effExpTime)
        
        
        print("Mapping...")
        Is_list=np.linspace(20,35,25)
        IcpIs_list=np.linspace(300,350,25)  #linspace(start,stop,number of steps)

        X,Y,im = plotLogLMap(lightCurveIntensityCounts, IcpIs_list, Is_list, effExpTime,IcPlusIs = True)
        

#############################################################################################
     
        
        effExpTime = .01
        lightCurveIntensityCounts, lightCurveIntensity, lightCurveTimes = getLightCurve(ts,ts[0]/1e6,ts[-1]/1e6,effExpTime)
        
        
        print("Mapping...")
        Is_list=np.linspace(20,35,25)
        IcpIs_list=np.linspace(300,350,25)  #linspace(start,stop,number of steps)

        X,Y,im = plotLogLMap(lightCurveIntensityCounts, IcpIs_list, Is_list, effExpTime,IcPlusIs = True)
        
#############################################################################################
        
        
#        effExpTime = .5
#        lightCurveIntensityCounts, lightCurveIntensity, lightCurveTimes = getLightCurve(ts,ts[0]/1e6,ts[-1]/1e6,effExpTime)
#        
#        
#        print("Mapping...")
#        Is_list=np.linspace(5,20,25)
#        IcpIs_list=np.linspace(300,350,25)  #linspace(start,stop,number of steps)
#
#        X,Y,im = plotLogLMap(lightCurveIntensityCounts, IcpIs_list, Is_list, effExpTime,IcPlusIs = True)
#        
##############################################################################################       
#        
#        
#        effExpTime = 1.
#        lightCurveIntensityCounts, lightCurveIntensity, lightCurveTimes = getLightCurve(ts,ts[0]/1e6,ts[-1]/1e6,effExpTime)
#        
#        
#        print("Mapping...")
#        Is_list=np.linspace(3,15,25)
#        IcpIs_list=np.linspace(300,350,25)  #linspace(start,stop,number of steps)
#
#        X,Y,im = plotLogLMap(lightCurveIntensityCounts, IcpIs_list, Is_list, effExpTime,IcPlusIs = True)
#        

        
    

    
    
    
    
    

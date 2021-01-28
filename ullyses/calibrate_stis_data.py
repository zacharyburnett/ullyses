#! /usr/bin/env python

import datetime
import argparse
import os
from astropy.io import fits
import shutil
import glob
import itertools
import numpy as np
import sys
import stistools
from stistools import x1d
from stistools.ocrreject import ocrreject
import stis_cti

from read_config import read_config, write_config
os.environ["oref"] = "/grp/hst/cdbs/oref/"

OREF_DIR = "/grp/hst/cdbs/oref"
SYM = "~"
NCOLS = 72
SEP = f"\n!{'~'*70}!\n"

class Stisdata():
    """
    A class to describe STIS data throughout the calibration process.

    Attributes:
        outdir (str): Path where output products should be written.
        infiles (list): List of input raw datasets.
        nfiles (int): Number of input raw datasets.
        basedir (str): Path to raw STIS data.
        rootname (dict): Dictionary where each key is a dictionary- each key
            is the rootname and vals describe dataset properties (detector,
            dither offset, actual file name).
        extract_info (dict): Nested dictionary where keys are rootnames; each
            nested dictionary describes dataset properties (visit, detector,
            XD shift, raw filename, flt filename, shifted flt filename, and
            combined image filename.
        _sci_dir (str): 'sci' directory used by stis_cti
        _dark_dir (str): 'dark' directory used by stis_cti
        _ref_dir (str): 'ref' directory used by stis_cti
#!!!        combined (dict): Nested dictionary where keys are the filenames of 
            the combined FLTs and FLCs; each nested dict listing the
            visit and detector values.
        x1d (list): List of final x1d products. 
        
    """
    def __init__(self, scifile, yamlfile, outdir=None):
        """
        Args:
            infiles: List or wildcard describing all input files.
            outdir: Output directory for products.
        """

        detector = fits.getval(scifile, "detector")
        opt_elem = fits.getval(scifile, "opt_elem")
        assert detector == "CCD" and opt_elem !="MIRVIS", f"Observing configurtion not supported: {detector}/{opt_elem}"
        self.scifile = scifile
        self.basedir = os.path.dirname(self.scifile)
        if outdir is None:
            nowdt = datetime.datetime.now()
            outdir = os.path.join(self.basedir, nowdt.strftime("%Y%m%d_%H%M"))
        self.outdir = outdir
        if not os.path.exists(outdir):
            os.mkdir(outdir)
        self.rootname = fits.getval(scifile, "rootname")
        self.flt = None
        self.crj = None
        self.x1d = None
        self.visit = self.rootname[4:6]
        self.target = fits.getval(scifile, "targname")
        self.flt = self.find_product("flc") 
        self.crj = self.find_product("crc") 
        self.x1d = self.find_product("x1d") 
        self.yamlfile = yamlfile
        self.config = read_config(yamlfile)
        self.x1d_c, self.fix_dq16, self.crrej_c, self.defringe_c, self.cti_proc = self.config["x1d"], self.config["fix_dq16"], self.config["crrej"], self.config["defringe"], self.config["processes"]
        self.fringeflat = self.defringe_c["fringeflat"]
        self.crsplit = fits.getval(scifile, "crsplit")
        self.opt_elem = opt_elem
        self._sci_dir = self.outdir
        self._dark_dir = "/astro/ullyses/stis_ccd_data/darks"
        self._ref_dir = "/astro/ullyses/stis_ccd_data/cti_refs"

#-----------------------------------------------------------------------------#

    def run_all(self):
        if self.flt is None:
            self.perform_cti()
        self.analyze_dark()
        #self.flag_negatives()
        self.check_crrej()
        if self.do_crrej is True:
            self.crrej()
        self.defringe()
        self.extract_spectra()
        write_config(self.config, self.yamlfile)
        self.help()

#-----------------------------------------------------------------------------#

    def flag_negatives(self, change_val=False, dq=4, thresh=-100):
        """
        For pixels with values below the input negative threshold, change the DQ
        value to a specified value. Also, if specified, change large negative 
        values to large positive values.
    
        Args:
            change_val (Bool): True if large negative pixel values should
                be chnaged to large positive values, default is False.
            dq (int): DQ value that large negative pixel values should
                be assigned, default is 4.
            thresh (int or float): Threshold below wich pixels should
                be flagged with specified DQ flag, default is -100.
        Returns:
            None
        """

        print("\n", f" FLAGGING NEGATIVES ".center(NCOLS, SYM), "\n")
        print(f"Flagging pixels with counts below {thresh} with DQ={dq}...")
        with fits.open(self.flt, mode="update") as sci_hdu: 
            sci_data = sci_hdu[1].data
            neg = np.where(sci_data <= thresh)
            sci_hdu[3].data[neg] += dq
            if change_val:
                print("Changing large negative values to large positive values...")
                sci_hdu[1].data[neg] = 10000
        
#-----------------------------------------------------------------------------#

    def analyze_dark(self, dq=16):
        """
        Create custom superdarks that are identical to those in FLT headers'
        except that they have correct dark-flagging done (STIS reference 
        pipeline does not do it correctly). Apply dark DQ (by default, DQ=16: 
        "Pixel having dark rate >5σ times the median dark level" -STIS IHB) 
        to FLTs.

        Args:
            dq (int): DQ to apply to pixels with large dark values.
        Returns:
            None 
        """

        self.fix_dq16 = False

        print("\n", f" CHECKING DQ=16 FLAGS ".center(NCOLS, SYM), "\n")
        customdark_dir = os.path.join(self.basedir, "custom_darks")
        if not os.path.isdir(customdark_dir):
            os.mkdir(customdark_dir)
            print(f"Made directory: {customdark_dir}")
            
        # Read in science FLT dataset.
        sci_hdu = fits.open(self.flt, mode="update")
        sci_dq = sci_hdu[3].data
        darkfile0 = sci_hdu[0].header["darkfile"]

        # Check how many pixels are flagged.
        # DQ=512 is "bad pixel in reference file"
        sci_dq16 = np.where((sci_dq&dq == dq) & (sci_dq&512 == 0))
        n_flagged = len(sci_dq16) * len(sci_dq16[0])
        total = len(sci_dq) * len(sci_dq[0])
        perc_flagged = n_flagged / total
        if perc_flagged <= 0.06:
            print(f"Less than 6% of pixels flagged with DQ=16, not performing custom dark correction")
            return
        else:
            self.fix_dq16 = True
            self.config["fix_dq16"] = True

            print(f"More than 6% of pixels ({perc_flagged*100.:.2f}) flagged with DQ=16")
            print(f"Manually creating superdarks and setting DQ={dq} values...")
    
        # Determine DARKFILE filename.
        if "/" in darkfile0:
            darkname = darkfile0.split("/")[1]
            darkfile = os.path.join(self._ref_dir, darkname)
        else:
            darkname = darkfile0.split("$")[1]
            darkfile = os.path.join(OREF_DIR, darkname)

        # Remove any existing DQ={dq} flags from FLT, since DQ={dq} flags are
        # inherently wrong.
        outfile = os.path.join(customdark_dir, darkname)
        sci_hdu[0].header["DARKFILE"] = outfile
        sci_hdu[3].data[sci_dq16] -= dq
        
        written_darks = [os.path.basename(x) for x in 
                         glob.glob(os.path.join(customdark_dir, "*fits"))]
        # If the custom darkfile has already been written (from a previous iteration), 
        # read from the darkfile and apply the DQ={dq} flags to the FLT. 
        if darkname in written_darks:
            dark_dq = fits.getdata(outfile, 3)
            dark_dq16 = np.where(dark_dq&dq == dq)
            sci_hdu[3].data[dark_dq16] |= dq
            sci_hdu.close()
            print(f"Already wrote custom dark: {darkname}")
        
        # Create custom darkfile, flag high dark values with dq={dq} 
        else:
            dark_hdu = fits.open(darkfile)
            dark = dark_hdu[1].data
            dark_dq = dark_hdu[3].data
            dark_dq16 = np.where(dark_dq&dq == dq)
            dark_dq[dark_dq16] -= dq
            sci_hdu[3].data[dark_dq16] |= dq
        
            # Determine 5*stddev + median of the darkfile, anything above this is DQ={dq}. 
            dark_thresh = (np.std(dark)*5) + np.median(dark)
            dark_inds = np.where((dark > dark_thresh) | (dark < -dark_thresh))
            dark_dq[dark_inds] |= dq
        
            new_dark = fits.HDUList( fits.PrimaryHDU() )
            new_dark[0].header = dark_hdu[0].header
            for ext in [1,2]:
                new_dark.append(fits.ImageHDU(dark_hdu[ext].data, dark_hdu[ext].header, name=dark_hdu[ext].name))
            new_dark.append(fits.ImageHDU(dark_dq, dark_hdu[3].header, name=dark_hdu[ext].name))
            new_dark.writeto(outfile)
        
            sci_hdu.close()
            dark_hdu.close()
        
            print(f"Wrote new darkfile: {outfile}")
                    
#-----------------------------------------------------------------------------#

    def perform_cti(self):
        """
        Run the STIS CTI code on STIS CCD data.
        """
        
        print("\n", f" PERFORMING CTI ".center(NCOLS, SYM), "\n")
        
        # These directories need to exist for stis_cti to run. 
        for direc in [self._dark_dir, self._ref_dir, self._sci_dir]:
            if not os.path.exists(direc):
                os.mkdir(direc)
                print(f"Made directory: {direc}")

#        # stis_cti needs raw, epc, spt, asn, and wav files as input.
#        print("Copying CCD datasets to science directory...")
#        allfiles = glob.glob(os.path.join(self.basedir, "*_raw.fits")) + \
#                   glob.glob(os.path.join(self.basedir, "*_epc.fits")) + \
#                   glob.glob(os.path.join(self.basedir, "*_spt.fits")) + \
#                   glob.glob(os.path.join(self.basedir, "*_asn.fits")) + \
#                   glob.glob(os.path.join(self.basedir, "*_wav.fits"))
#        for item in allfiles:
#            shutil.copy(item, self._sci_dir)
    
        # Run stis_cti
        stis_cti.stis_cti(self._sci_dir, self._dark_dir, self._ref_dir, self.cti_proc, verbose=True, clean=True)
        self.flt = os.path.join(self.outdir, self.rootname+"_flc.fits")
        self.crj = os.path.join(self.outdir, self.rootname+"_crc.fits")
        
#        self.copy_products()

#-----------------------------------------------------------------------------#

    def copy_products(self):
        """
        Copy the intermediate products from STIS CTI products to self.outdir, 
        since several calibration iterations are often needed and we don't want
        to overwrite the original products.
        """

        if os.path.exists(self.outdir):
            print("WARNING: Output directory already exists, deleting {0}".format(self.outdir))
            shutil.rmtree(self.outdir)
        os.mkdir(self.outdir)
    
        # If stis_cti was not run, define the stis_cti products directories.
        if not hasattr(self, "_sci_dir"):
            self._sci_dir = os.path.join(self.basedir, "science")
            self._dark_dir = os.path.join(self.basedir, "darks")
            self._ref_dir = os.path.join(self.basedir, "ref")
       
        # For CCD data, copy FLCs. For MAMA data, copy FLTs.
        flc_files = glob.glob(os.path.join(self._sci_dir, "*flc.fits"))
        crc_files = glob.glob(os.path.join(self._sci_dir, "*crc.fits"))
        for item in flc_files+crc_files:
            shutil.copy(item, self.outdir)
        
        print(f"Copied FLC and CRC files to {self.outdir}")
        self.flt = os.path.join(self.outdir, self.rootname+"_flc.fits")
        self.crj = os.path.join(self.outdir, self.rootname+"_crc.fits")
        
#-----------------------------------------------------------------------------#

    def extract_spectra(self):
        """
        Extract the spectra using stistools.x1d.
        """

        print("\n", f" EXTRACTING SPECTRA ".center(NCOLS, SYM), "\n")
        
        # Look up extraction parameters given the target config file.
        self.nonsci_x1d = []
        for targ, pars in self.x1d_c.items():
            if targ == "sci":
                outfile = os.path.join(self.outdir, self.rootname+"_x1d.fits")
                self.x1d = outfile
            else:
                outfile = os.path.join(self.outdir, f"{self.rootname}_{targ}_x1d.fits")
                self.nonsci_x1d.append(outfile)
            if os.path.exists(outfile):
                os.remove(outfile)
            x1d.x1d(self.drj, 
                output = outfile, 
                a2center = pars["yloc"], 
                maxsrch = pars["maxsrch"],
                extrsize = pars["height"],
                bk1offst = pars["b_bkg1"] - pars["yloc"],
                bk2offst = pars["b_bkg2"] - pars["yloc"],
                bk1size = pars["b_hgt1"],
                bk2size = pars["b_hgt2"],
                ctecorr="omit",
                verbose=True)
            print(f"Wrote x1d file: {outfile}")

#-----------------------------------------------------------------------------#

    def check_crrej(self):
        
        print("\n", f" CHECKING CR REJECTION ".center(NCOLS, SYM), "\n")
        
        self.do_crrej = False
        if self.x1d is None:
            print("No x1d files specified")
            return

        x1d_data = fits.getdata(self.x1d)[0]
        extr_mask = np.zeros((1024, 1024))
        del_pix = x1d_data["EXTRSIZE"]/2.
        for column in range(1024):
            row_mid =  x1d_data['EXTRLOCY'][column] - 1 #EXTRLOCY is 1-indexed.
            gd_row_low = int(np.ceil( row_mid - del_pix))
            gd_row_high = int(np.floor(row_mid + del_pix))
            extr_mask[gd_row_low:gd_row_high+1,column] = 1
        n_tot = np.count_nonzero(extr_mask) * self.crsplit

        n_rej = []
        with fits.open(self.flt) as flt_hdu:
            for i in range(3,len(flt_hdu)+1 ,3):
                flt_data = flt_hdu[i].data
                rej = flt_data[ (extr_mask == 1) & (flt_data & 8192 != 0)] #Data quality flag 8192 (2^13) used for CR rejected pixels
                n_rej.append(np.count_nonzero(rej))

        t_exp = float(fits.getval(self.x1d, "texptime"))
        rej_rate = float(fits.getval(self.x1d, "rej_rate"))

        # Calculate the rejection fraction and the rate of rejected pixels per sec
        n_pix_rej = np.sum(np.array(n_rej))
        frac_rej = n_pix_rej/float(n_tot)
        rej_rate = n_pix_rej * 1024.0**2 / (t_exp * n_tot)

        # Calculate the expected rejection fraction rate given the rate in header
        # This is the rej_rate * ratio of number of pixels in full CCD vs extract region (n_tot /CRSPLIT)
        frac_rej_expec =  rej_rate * t_exp /(1024.*1024.*self.crsplit)

        print('Percentage of pixels rejected as CRs')          
        print(f'   Extraction Region: {frac_rej:.2f}')
        print(f'   Full CCD Frame: {frac_rej_expec:.2f} \n')

        if frac_rej < (frac_rej_expec * 5) or frac_rej < .05:
            print("Extraction region rejection rate is not high enough for custom CR rejection")
            return
        else:
            self.do_crrej = True
            self.config["do_crrej"] = True

#-----------------------------------------------------------------------------#

    def crrej(self):
        """
        Check on CR rejection rate comes from Joleen Carlberg's code: 
        https://github.com/spacetelescope/stistools/blob/jkc_cr_analysis/stistools/crrej_exam.py
        """
       
        print("\n", f" PERFORMING CR REJECTION ".center(NCOLS, SYM), "\n")
        
        if self.x1d is None:
            print("No x1d files specified")
            return

        outfile = os.path.join(self.outdir, self.rootname+"_crc.fits")
        if os.path.exists(outfile):
            os.remove(outfile)
        
        # Look up crrej parameters given the target config file.
        ocrreject(self.flt,
                  output=outfile,
                  initgues=self.crrej_c["initgues"],
                  crsigmas=self.crrej_c["crsigmas"],
                  crradius=self.crrej_c["crradius"],
                  crthresh=self.crrej_c["crthresh"],
                  crmask=self.crrej_c["crmask"],
                  verbose=self.crrej_c["verbose"])

        print(f"Wrote crj file: {outfile}")
        self.crj = outfile


#-----------------------------------------------------------------------------#

    def defringe(self):

        print("\n", f" DEFRINGING DATA ".center(NCOLS, SYM), "\n")
        
        fringeflat = self.defringe_c["fringeflat"]
        rawfringe = os.path.join(self.basedir, fringeflat)
        fringeroot = os.path.basename(fringeflat)[:9]
        outnorm = os.path.join(self.outdir, fringeroot+"_nsp.fits")
        if os.path.exists(outnorm):
            os.remove(outnorm)
        stistools.defringe.normspflat(inflat=rawfringe,
                   do_cal=True,
                   outflat=outnorm)
        outmk = os.path.join(self.outdir, fringeroot+"_mff.fits")
        if os.path.exists(outmk):
            os.remove(outmk)
        stistools.defringe.mkfringeflat(inspec=self.crj,
                     inflat=outnorm,
                     outflat=outmk,
                     do_shift=self.defringe_c["mkfringeflat"]["do_shift"],
                     beg_shift=self.defringe_c["mkfringeflat"]["beg_shift"],
                     end_shift=self.defringe_c["mkfringeflat"]["end_shift"],
                     shift_step=self.defringe_c["mkfringeflat"]["shift_step"],
                     do_scale=self.defringe_c["mkfringeflat"]["do_scale"],
                     beg_scale=self.defringe_c["mkfringeflat"]["beg_scale"],
                     end_scale=self.defringe_c["mkfringeflat"]["end_scale"],
                     scale_step=self.defringe_c["mkfringeflat"]["scale_step"])
        outfile = stistools.defringe.defringe(science_file=self.crj,
                           fringe_flat=outmk,
                           overwrite=True,
                           verbose=True)

        print(f"Wrote defringed crj file: {outfile}")
        shutil.copy(outfile, self.outdir)
        self.drj = os.path.join(self.outdir, os.path.basename(outfile))
        

#-----------------------------------------------------------------------------#

    def find_product(self, ext):
        ext2 = {"x1d": "sx1", "sx1": "x1d", 
                "flt": "flc", "flc": "flt", 
                "crj": "crc", "crc": "crj"}
        prod = os.path.join(self.basedir, self.rootname+"_"+ext+".fits")
        if not os.path.exists(prod):
            prod = os.path.join(self.outdir, self.rootname+"_"+ext+".fits")
            if not os.path.exists(prod):
                prod = os.path.join(self.basedir, self.rootname+"_"+ext2[ext]+".fits")
                if not os.path.exists(prod):
                    prod = os.path.join(self.outdir, self.rootname+"_"+ext2[ext]+".fits")
                    if not os.path.exists(prod):
                        prod = None
    
        return prod

#-----------------------------------------------------------------------------#

    def help(self):
        print("\n", f" RECALIBRATION SUMMARY ".center(NCOLS, SYM), "\n")
        print(f"Raw file: {self.scifile}")
        print(f"FLT/FLC: {self.flt}")
        print(f"CRJ/CRC: {self.crj}")
        print(f"Science X1D: {self.x1d}")
        if len(self.nonsci_x1d) > 0:
            print(f"Non-science X1D(s): {self.nonsci_x1d}")

        print("")
        if self.flt is not None:
            print("CTI correction already performed")
        else:
            print("WARNING!!! CTI correction needs to be performed")
        if self.do_crrej is True:
            print("WARNING!!! CR rejection should be performed again. Edit yaml file and re-run.")
        else:
            print("Default CR rejection parameters were used")
        if self.fix_dq16 is True:
            print("Custom DQ=16 flagging has been applied")
        else:
            print("Default DQ=16 flagging was used")

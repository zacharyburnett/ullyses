# For info on how to write a setup.py file, check out the link below 
# or ask a friendly neighborhood python programmer! 
# https://docs.python.org/3.7/distutils/setupscript.html

from setuptools import setup, find_packages
import glob

setup(
    name = "ullyses",
    version = "0.0.1",
    description = "Create ULLYSES data products",
    author = "ULLYSES STScI Team",
    keywords = ["astronomy"],
    classifiers = ['Programming Language :: Python',
                   'Programming Language :: Python :: 3',
                   'Development Status :: 1 - Planning',
                   'Intended Audience :: Science/Research',
                   'Topic :: Scientific/Engineering :: Astronomy',
                   'Topic :: Scientific/Engineering :: Physics',
                   'Topic :: Software Development :: Libraries :: Python Modules'],
    packages = ["ullyses"],
    package_dir = {"ullyses": "ullyses"},
    install_requires = ["setuptools",
                        "numpy",
                        "astropy",
						"ullyses_utils"
                        ],
    # If you need something from github, any of the below formats work:
#    dependency_links = ["git+ssh://git@github.com/npirzkal/GRISMCONF.git#egg=GRISMCONF-0.1"],
#    dependency_links = ["git+git@github.com:npirzkal/NIRCAM_Gsim@master#egg=NIRCAM_Gsim-0.1"],
#    dependency_links = ["git+https://github.com/npirzkal/NIRCAM_Gsim@master#egg=NIRCAM_Gsim-0.1"],
    dependency_links = ["git+https://github.com/spacetelescope/ullyses-utils@main#egg=ullyses_utils-0.1"],
    )
from __future__ import print_function
import setuptools, sys
import os
from setuptools.command.install import install
from setuptools.command.develop import develop
import subprocess
import numpy

#pip install -e git+http://github.com/mazinlab/mkidpipeline.git@develop#egg=mkidpipeline

from setuptools.extension import Extension
from Cython.Build import cythonize
from Cython.Distutils import build_ext

parsebin_extension = Extension(
    name="mkidpipeline.hdf.parsebin",
    sources=["mkidpipeline/hdf/parsebin.pyx","mkidpipeline/hdf/binlib.c"],
    library_dirs=["mkidpipeline/hdf"], # Location of .o file
    include_dirs=["mkidpipeline/hdf",numpy.get_include()], # Location of the .h file
    extra_compile_args=["-std=c99"]
)

gen_photon_list_extension = Extension(
    name="mkidpipeline.speckle.photonstats_utils",
    sources=['mkidpipeline/speckle/photonstats_utils.pyx']
)

def compile_and_install_software():
    """Used the subprocess module to compile/install the C software."""
    src_path = './mkidpipeline/hdf/'
    try:
        subprocess.check_call('/usr/local/hdf5/bin/h5cc -shlib -pthread -g -o bin2hdf bin2hdf.c',
                              cwd=src_path, shell=True)

    except Exception as e:
        print(str(e))
        #raise e don't raise because on some machines h5cc might not exist.


class CustomInstall(install, object):
    """Custom handler for the 'install' command."""
    def run(self):
        compile_and_install_software()
        super(CustomInstall,self).run()


class CustomDevelop(develop, object):
    """Custom handler for the 'install' command."""
    def run(self):
        compile_and_install_software()
        super(CustomDevelop,self).run()


with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="mkidpipeline",
    version="0.0.1",
    author="MazinLab",
    author_email="mazinlab@ucsb.edu",
    description="An UVOIR MKID Data Reduction Package",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/MazinLab/MKIDPipeline",
    packages=setuptools.find_packages(),
    ext_modules=cythonize([parsebin_extension,gen_photon_list_extension]),
    classifiers=(
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX",
        "Development Status :: 1 - Planning",
        "Intended Audience :: Science/Research"),
    zip_safe=False,
    cmdclass={'install': CustomInstall,'develop': CustomDevelop}
)
#!/usr/bin/env python
'''
Created on 2015/11/17

:author: hubo
'''
try:
    import ez_setup
    ez_setup.use_setuptools()
except:
    pass
from setuptools import setup, find_packages

VERSION = '0.2.4'

setup(name='vlcp-docker-plugin',
      version=VERSION,
      description='Docker network plugin for VLCP',
      author='Hu Bo',
      author_email='hubo1016@126.com',
      license="http://www.apache.org/licenses/LICENSE-2.0",
      url='https://github.com/hubo1016/vlcp-docker-plugin',
      keywords=['SDN', 'VLCP', 'docker'],
      test_suite = 'tests',
      use_2to3=False,
      install_requires = ["vlcp>=1.2.3"],
      packages=find_packages(exclude=("tests","tests.*","misc","misc.*")))

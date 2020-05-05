#!/usr/bin/env python3

# The MIT License (MIT)
# Copyright (c) 2020 by Brockmann Consult GmbH
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
# of the Software, and to permit persons to whom the Software is furnished to do
# so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.


from setuptools import setup, find_packages

requirements = [
    # Sync with ./environment.yml.
]

packages = find_packages(exclude=["test", "test.*"])

# Same effect as "from xcube_cci import version", but avoids importing xcube_cci:
version = None
with open('xcube_cci/version.py') as f:
    exec(f.read())

setup(
    name="xcube_cci",
    version=version,
    description='An xcube plugin that allows generating data cubes from the ESA CCI Open Data Portal',
    license='MIT',
    author='xcube Development Team',
    packages=packages,
    include_package_data=True,
    install_requires=requirements,
)

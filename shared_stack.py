#!/usr/bin/env python
#
# LSST Data Management System
#
# Copyright 2008-2016  AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.
#
"""
Shared-stack maintenance.

This tool builds and maintains a "shared stack" installation of the LSST
Science Pipelines. It is designed to run on the LSST developer infrastructure
(``lsst-dev``, etc); it may be useful elsewhere.

End users looking to install the stack for personal use should prefer the
procedure described on the `DM Website <http://dm.lsst.org>`_. Developers
looking to build the latest versions of the LSST code should prefer the `build
tool <http://developer.lsst.io/en/latest/build-ci/lsstsw.html>`_.

Specifically, when pointed (``ROOT``, defined below) at a
directory which does not exist, we:

- Install EUPS into that directory;
- Use ``eups distrib`` to install Miniconda into the directory;
- Use ``conda install`` to update that Miniconda to a full Anaconda
  installation;
- Create shell initialization scripts which set up EUPS and the Anaconda
  Python installation (``loadLSST.bash``, ``.csh``, ``.ksh``, ``.zsh``).
- Proceed with the stack maintenance procedure, defined below.

When ``ROOT`` exists and contains an installed version of EUPS, the following
maintainance procedure is followed:

- Contact the EUPS distribution server (``EUPS_PKGROOT``, below), retrieving a
  the contents of all tags that match the ``VERSION_GLOB`` expression.
- For all specified products (``PRODUCTS``, below), identify tags retrieved
  from the server which have not been installed and install them.
- Sort the installed tags by date they were created on the server and tag the
  most recent as "current".

This tool requires Python (tested with 2.6, 2.7 and 3.5) and `lxml
<http://lxml.de/>`_; the latter may be conveniently installed using ``pip``::

  $ pip install -r requirements.txt

All configuration is performed by editing the ``CONFIGURATION`` block below;
there are no command line options at this time.
"""
from __future__ import print_function

import os
import shutil
import re
import subprocess
import tarfile
import tempfile
from datetime import datetime
from lxml import html
from textwrap import dedent
try:
    # Python 3
    from urllib.request import urlopen
except ImportError:
    # Python 2
    from urllib2 import urlopen

#
# CONFIGURATION
#

# Set to True to output detailed information on commands being executed and
# their environment.
DEBUG = True

# Package distribution server to use.
EUPS_PKGROOT = "https://sw.lsstcorp.org/eupspkg/"

# Version of the EUPS to install when creating a new stack. Should correspond
# to a tag defined at https://github.com/RobertLuptonTheGood/eups.
EUPS_VERSION = "2.0.2"

# Version of LSST's miniconda2 package to install. Should correspond to a
# version distributed through ``EUPS_PKGROOT``.
MINICONDA2_VERSION = "3.19.0.lsst4"

# Version of Anaconda to install. Should correspond to the version of
# Miniconda defined above.
ANACONDA_VERSION = "2.5.0"

# Top-level products to install into the stack.
PRODUCTS = ["lsst_distrib"]

# Root directory in which the stack will be created or updated.
ROOT = '/ssd/lsstsw/stack'

# Only tags matching this regular expression will be fetched from
# ``EUPS_PKGROOT`` and hence considered for local installation. The more tags
# are matched, the slower things will be.
VERSION_GLOB = r"w_2016_\d\d|v12_\d(_rc\d)?"


def determine_flavor():
    """
    Return a string representing the 'flavor' of the local system.

    Based on the equivalent logic in EUPS, but without introducing an EUPS
    dependency.
    """
    uname, machine = os.uname()[0:5:4]
    if uname == "Linux":
        if machine[-2:] == "64":
            return "Linux64"
        else:
            return "Linux"
    elif uname == "Darwin":
        if machine in ("x86_64", "i686"):
            return "DarwinX86"
        else:
            return "Darwin"
    else:
        raise RuntimeError("Unknown flavor: (%s, %s)" % (uname, machine))


class Product(object):
    """
    Information about a particular EUPS product.

    This includes the the product name, the available versions and their
    associated tags (if any).
    """
    def __init__(self, name):
        self.name = name

        # Map from version to tags corresponding to that version.
        # NB cannot use a default dict, because we need to distinguish between
        # versions which have no tags and versions which do not exist.
        self._versions = {}

    def add_version(self, version):
        if version not in self._versions:
            self._versions[version] = set()

    def add_tag(self, version, tag):
        self._versions[version].add(tag)

    def versions(self, tag=None):
        """
        Return a list of versions of the product. If ``tag`` is not ``None``,
        return only those versions tagged ``tag``.
        """
        if tag is None:
            return self._versions.keys()
        else:
            return [k for k, v in self._versions.items() if tag in v]

    def tags(self, version=None):
        """
        Return a list of tags applied to the product. If ``version`` is not
        ``None``, return only those tags which refer to ``version``.
        """
        if version is None:
            return set.union(*self._versions.values())
        else:
            return self._versions[version]


class ProductTracker(object):
    """
    Track a collection of Products.
    """
    def __init__(self):
        self._products = {}

    def tags_for_product(self, product_name):
        """
        Return the set of all tags which contain a product
        named ``product_name``.
        """
        try:
            return self._products[product_name].tags()
        except KeyError:
            return set()

    def products_for_tag(self, tag):
        """
        Return a list of (product_name, version) tuples which are tagged with
        ``tag``.
        """
        results = []
        for product in self._products.values():
            versions = product.versions(tag=tag)
            for version in versions:
                results.append((product.name, version))
        return results

    def current(self, product_name):
        """
        Return the version of product_name which is tagged "current", or None.
        """
        if product_name in self._products:
            return self._products[product_name].versions("current")[0]

    def has_version(self, product_name, version):
        """
        Return True if we have the given version of product name.
        """
        return (product_name in self._products and
                version in self._products[product_name].versions())

    def insert(self, product, version, tag=None):
        """
        Add (product, version, tag) to the list of products being tracked.
        """
        if product not in self._products:
            self._products[product] = Product(product)
        self._products[product].add_version(version)
        if tag:
            self._products[product].add_tag(version, tag)


class RepositoryManager(object):
    """
    Provide access to a ProductTracker built on a remote repository.
    """
    def __init__(self, pkgroot=EUPS_PKGROOT, pattern=r".*"):
        """
        Only tags which match regular expression ``pattern`` are recorded.
        More tags -> slower loading.
        """
        self._product_tracker = ProductTracker()
        self.tag_dates = {}
        self.pkgroot = pkgroot

        h = html.parse(urlopen(self.pkgroot + "/tags"))
        for el in h.findall("./body/pre/a"):
            if el.text[-5:] == ".list" and re.match(pattern, el.text):
                u = urlopen(pkgroot + '/tags/' + el.get('href'))
                tag_date = datetime.strptime(u.info()['last-modified'],
                                             "%a, %d %b %Y %H:%M:%S %Z")
                self.tag_dates[el.text[:-5]] = tag_date
                for line in u.read().decode('utf-8').strip().split('\n'):
                    if ("EUPS distribution %s version list" %
                       (el.text[:-5]) in line):
                        continue
                    if line.strip()[0] == "#":
                        continue
                    product, flavor, version = line.split()
                    self._product_tracker.insert(product, version,
                                                 el.text[:-5])

    def tags_for_product(self, product_name):
        return self._product_tracker.tags_for_product(product_name)

    def products_for_tag(self, tag):
        return self._product_tracker.products_for_tag(tag)


class StackManager(object):
    """
    Tools for working with an EUPS product stack.

    Includes the functionality of a ProductTracker together with routines for
    creating and manipulating the stack.
    """
    def __init__(self, stack_dir, pkgroot=EUPS_PKGROOT,
                 userdata=None, debug=DEBUG):
        """
        Create a StackManager to manage the stack in ``stack_dir``.

        ``stack_dir`` should already exist and contain an EUPS installation
        (see StackManager.create_stack() if it doesn't).

        Use the remote ``pkgroot`` as a distribution server when installing
        new products.

        Store user data (e.g. the EUPS cache) in ``userdata``, rather than the
        current user's home directory, if supplied. This means that multiple
        StackManagers can be operated by the same user simultaneously without
        conflict.

        Write verbose debugging information if ``debug`` is ``True``.
        """
        self.stack_dir = stack_dir
        self.flavor = determine_flavor()

        # Generate extra output
        self.debug = debug

        # Generate a minimal working environment for EUPS; best guess without
        # going through setups.sh.
        self.eups_environ = os.environ.copy()
        self.eups_environ.update({
            "PATH": "%s:%s" % (os.path.join(stack_dir, "eups", "bin"),
                               self.eups_environ['PATH']),
            "EUPS_PATH": stack_dir,
            "EUPS_DIR": os.path.join(stack_dir, "eups"),
            "EUPS_SHELL": "sh",
            "PYTHONPATH": os.path.join(stack_dir, "eups", "python"),
            "SETUP_EUPS": ("eups LOCAL:%s -f (none) -Z (none)" %
                           (os.path.join(stack_dir, "eups"),)),
            "EUPS_PKGROOT": pkgroot
        })
        if userdata:
            self.eups_environ["EUPS_USERDATA"] = userdata

        self._refresh_products()

    def _refresh_products(self):
        """
        Update the list of products we track in this stack.

        Should be run whenever the stack state is changed (e.g. by installing
        new products).
        """
        self._product_tracker = ProductTracker()

        for line in self._run_cmd("list", "--raw").strip().split('\n'):
            if line == '':
                continue
            product, version, tags = line.split("|")
            if tags == '':
                self._product_tracker.insert(product, version)
            for tag in tags.split(":"):
                if tag in ("setup"):
                    continue
                self._product_tracker.insert(product, version, tag)

        # If a current version of miniconda2 is available, add it to our
        # environment.
        try:
            miniconda_version = self._product_tracker.current("miniconda2")
        except IndexError:
            miniconda_version = None
        if miniconda_version:
            miniconda_path = os.path.join(self.stack_dir, self.flavor,
                                          "miniconda2", miniconda_version)
            self.eups_environ["PATH"] = "%s:%s" % (os.path.join(miniconda_path,
                                                                "bin"),
                                                   self.eups_environ["PATH"])

    def _run_cmd(self, cmd, *args):
        """
        Run an ``eups`` command to manipulate the local stack.
        """
        to_exec = ['eups', '--nolocks', cmd]
        to_exec.extend(args)
        if self.debug:
            print(self.eups_environ)
            print(to_exec)
        return StackManager._check_output(to_exec, env=self.eups_environ,
                                          universal_newlines=True)

    def conda(self, action, package_name, version=None):
        """
        Perform ``action`` ("install", "remove", etc) on package named
        ``package_name``. If supplied, version is appended to the package name
        (thus ``package_name=version``).

        Returns the output from executing the command.
        """
        if not self._product_tracker.current("miniconda2"):
            print("Miniconda not available; cannot %s %s" %
                  (action, package_name))
            return
        if version:
            package = "%s=%s" % (package_name, version)
        else:
            package = package_name
        to_exec = ["conda", action, "--yes", package]
        if self.debug:
            print(self.eups_environ)
            print(to_exec)
        return StackManager._check_output(to_exec, env=self.eups_environ,
                                          universal_newlines=True)

    def tags_for_product(self, product_name):
        return self._product_tracker.tags_for_product(product_name)

    def version_from_tag(self, product_name, tag):
        """
        Return the version of ``product_name`` which is tagged ``tag``.
        """
        for product, version in self._product_tracker.products_for_tag(tag):
            if product == product_name:
                return version

    def distrib_install(self, product_name, version=None, tag=None):
        """
        Use ``eups distrib`` to install ``product_name``.

        If ``version`` and/or ``tag`` are specified, ask for them explicitly.
        Otherwise, accept the defaults.
        """
        args = ["install", "--no-server-tags", product_name]
        if version:
            args.append(version)
        if tag:
            args.extend(["-t", tag])
        print(self._run_cmd("distrib", *args))
        self._refresh_products()

    def add_global_tag(self, tagname):
        """
        Add a global tag to the stack's startup.py file.

        Note that it is -- with some exceptions -- only possible to tag
        products with tags that have been pre-declared in startup.py.
        Therefore, we need to call this before we can use ``apply_tag()``.
        """
        startup_path = os.path.join(self.stack_dir, "site", "startup.py")
        with open(startup_path, "a") as startup_py:
            startup_py.write('hooks.config.Eups.globalTags += ["%s"]\n' %
                             (tagname,))

    def tags(self):
        """
        Return a list of all tags in the stack.
        """
        return self._run_cmd("tags").split()

    def apply_tag(self, product_name, version, tagname):
        """
        Apply ``tagname`` to ``version`` of ``product_name``.

        Note that ``tagname`` must generally have been
        pre-declared using ``add_global_tag()``.
        """
        if self._product_tracker.has_version(product_name, version):
            self._run_cmd("declare", "-t", tagname, product_name, version)
            self._product_tracker.insert(product_name, version, tagname)

    @staticmethod
    def create_stack(stack_dir, pkgroot=EUPS_PKGROOT, userdata=None,
                     python="/usr/bin/python", debug=DEBUG):
        """
        Bootstrap a stack in ``stack_dir``.

        ``stack_dir`` should not already exist. We will install EUPS and
        Anaconda in it, returning an initialized StackManager.

        The ``python`` argument is only used for bootstrapping EUPS: for
        working with the stack, we will use Anaconda.

        Other arguments are as for ``StackManager.__init__()``.
        """
        # Refuses to proceed if ``stack_dir`` already exists.
        os.makedirs(stack_dir)

        # Install EUPS into the stack directory.
        EUPS_URL = "https://github.com/RobertLuptonTheGood/eups/archive/%s.tar.gz" % (EUPS_VERSION,)
        eups_download = urlopen(EUPS_URL)
        tf = tarfile.open(fileobj=eups_download, mode="r|gz")
        eups_build_dir = tempfile.mkdtemp()
        try:
            tf.extractall(eups_build_dir)
            StackManager._check_output(["./configure",
                                        "-prefix=%s/eups" % (stack_dir,),
                                        "--with-eups=%s" % (stack_dir,),
                                        "--with-python=%s" % (python,)],
                                       cwd=os.path.join(eups_build_dir,
                                                        "eups-%s" % (EUPS_VERSION,)))
            StackManager._check_output(["make", "install"],
                                       cwd=os.path.join(eups_build_dir,
                                                        "eups-%s" % (EUPS_VERSION,)))
            if debug:
                print("Done installing EUPS %s" % (EUPS_VERSION,))
        finally:
            shutil.rmtree(eups_build_dir)

        sm = StackManager(stack_dir, pkgroot=pkgroot,
                          userdata=userdata, debug=debug)
        sm.distrib_install("miniconda2", version=MINICONDA2_VERSION)
        sm.apply_tag("miniconda2", MINICONDA2_VERSION, "current")
        if debug:
            print("Miniconda installed.")

        sm.conda("install", "anaconda", ANACONDA_VERSION)
        for package in "nomkl numpy scipy scikit-learn numexpr".split():
            sm.conda("install", package)
        for package in "mkl mkl-service".split():
            sm.conda("remove", package)
        # Set the permissions on the Anaconda dir to avoid end users
        # creating undeletable .pyc files.
        StackManager._check_output(["chmod", "-R", "g-w",
                                    os.path.join(stack_dir, determine_flavor(),
                                                 "miniconda2",
                                                 MINICONDA2_VERSION)])
        if debug:
            print("Upgraded to Anaconda %s" % (ANACONDA_VERSION,))

        loader_template = dedent("""
        source %s
        setup miniconda2
        """).strip()
        for lsstSuffix, eupsSuffix in (('bash', 'sh'),
                                       ('csh', 'csh'),
                                       ('ksh', 'sh'),
                                       ('zsh', 'zsh')):
            with open(os.path.join(stack_dir,
                      "loadLSST.%s" % (lsstSuffix,)), 'w') as f:
                f.write(loader_template %
                        (os.path.join(stack_dir, "eups", "bin",
                                      "setups.%s" % (eupsSuffix,))))

        sm.distrib_install("lsst")
        return sm

    @staticmethod
    def _check_output(*popenargs, **kwargs):
        """
        Run an external command, check its exit status, and return its output.
        """
        # This is effectively  subprocess.check_output() function from
        # Python 2.7+ provided here for compatibility with Python 2.6.
        process = subprocess.Popen(stdout=subprocess.PIPE,
                                   *popenargs, **kwargs)
        output, unused_err = process.communicate()
        retcode = process.poll()
        if retcode:
            cmd = kwargs.get("args")
            print("Failed process output:")
            print(output)
            if cmd is None:
                cmd = popenargs[0]
            raise subprocess.CalledProcessError(retcode, cmd)
        return output


def main():
    # We create a temporary directory for the EUPS cache etc. This means we
    # can run multiple instances of StackManager simultaneously without them
    # clobbering each other.
    userdata = tempfile.mkdtemp()

    # If the stack doesn't already exist, create it.
    if not os.path.exists(ROOT):
        sm = StackManager.create_stack(ROOT, userdata=userdata)
    else:
        sm = StackManager(ROOT, userdata=userdata)

    rm = RepositoryManager(pattern=VERSION_GLOB)

    for product in PRODUCTS:
        print("Considering %s" % (product,))
        server_tags = rm.tags_for_product(product)
        installed_tags = sm.tags_for_product(product)
        candidate_tags = server_tags - installed_tags

        for tag in candidate_tags:
            print("  Installing %s tagged %s" % (product, tag))
            sm.distrib_install(product, tag=tag)
            if tag not in sm.tags():
                print("  Adding global tag %s" % (tag,))
                sm.add_global_tag(tag)

            print("  Applying tag %s" % (tag,))
            for sub_product, version in rm.products_for_tag(tag):
                sm.apply_tag(sub_product, version, tag)

        # Tag as current based on date ordering on server.
        available_tags = server_tags.intersection(sm.tags_for_product(product))
        if available_tags:  # Could be an empty set
            current_tag = max(available_tags,
                              key=lambda tag: rm.tag_dates[tag])
            print("  Marking %s %s as current" % (product, current_tag))
            for sub_product, version in rm.products_for_tag(current_tag):
                sm.apply_tag(sub_product, version, "current")

    shutil.rmtree(userdata)


if __name__ == "__main__":
    main()

#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import re
import tempfile
import platform
import os

try:
    # Python 3
    from urllib.request import urlopen
except ImportError:
    # Python 2
    from urllib2 import urlopen

EUPS_PKGROOT = "https://sw.lsstcorp.org/eupspkg/"
VERSION_GLOB = r"w_2016_\d\d|v12_\d(_rc\d)?"
VERSION_GLOB = r"v12_\d(_rc\d)?"
PRODUCTS = ["lsst_distrib"]
DEBUG = True
ROOT = '/tmp'

def determine_flavor():
    """
    Return a string representing the 'flavor' of the local system.

    Based on the equivalent logic in EUPS, but without introducing an EUPS
    dependency.
    """
    uname, machine = platform.uname()[0:5:4]

    print(uname)
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
    elif uname == "Windows":
        return "windows"
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

        h = urlopen(pkgroot + "tags/")
        for tagline in h.read().decode('utf-8').strip().split('\n'):
            ws = tagline.split('"')
            if len(ws) > 5:
                tag = ws[6][1:]
                if '.list' not in tag:
                    continue
                if not re.match(pattern, tag):
                    continue

                tag = tag.split('.list')[0]

                print("-----------------", tag)

                try:
                    u = urlopen("https://sw.lsstcorp.org/eupspkg/tags/%s.list" % tag)
                except:
                    continue

                # tag_date = datetime.strptime(u.info()['last-modified'], "%a, %d %b %Y %H:%M:%S %Z")

                for line in u.read().decode('utf-8').strip().split('\n'):

                    if "EUPS distribution " in line:
                        continue
                    if line.strip()[0] == "#":
                        continue

                    product, flavor, version = line.split()
                    print('>', product, flavor, version)
                    self._product_tracker.insert(product, version)

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
    def __init__(self, stack_dir, pkgroot=EUPS_PKGROOT, userdata=None, debug=DEBUG):
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

        print('StackManager', stack_dir, pkgroot, userdata, debug)

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
    def _check_output(*popenargs, **kwargs):
        """
        Run an external command, check its exit status, and return its output.
        """
        # This is effectively  subprocess.check_output() function from
        # Python 2.7+ provided here for compatibility with Python 2.6.
        print(popenargs, kwargs)
        return "ok"

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


if __name__ == '__main__':

    pt = ProductTracker()

    pattern = VERSION_GLOB

    userdata = tempfile.mkdtemp()

    # If the stack doesn't already exist, create it.
    if not os.path.exists(ROOT):
        sm = StackManager.create_stack(ROOT, userdata=userdata)
    else:
        sm = StackManager(ROOT, userdata=userdata)

    rm = RepositoryManager(pkgroot=EUPS_PKGROOT, pattern=pattern)

    for product in PRODUCTS:
        print("Considering %s" % (product,))
        server_tags = rm.tags_for_product(product)
        installed_tags = sm.tags_for_product(product)
        candidate_tags = server_tags - installed_tags

        for tag in candidate_tags:
            print("  Installing %s tagged %s" % (product, tag))


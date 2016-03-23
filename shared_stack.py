import os
import shutil
from lxml import html
from urllib2 import urlopen
import re
import subprocess
import tarfile
import tempfile

# Configuration
EUPS_VERSION = "2.0.1"
MINICONDA2_VERSION = "3.19.0.lsst4" # Or most recent?
CONDA_PACKAGES = ["jupyter"] # In addition to the default LSST install
PRODUCTS = ["lsst_apps"]

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
        raise RuntimeError, ("Unknown flavor: (%s, %s)" % (uname, machine))


class Product(object):
    def __init__(self, name):
        self.name = name

        # Map from version to tags corresponding to that version.
        # NB cannot use a default dict, because we need to distinguish between
        # versions which have no tags and versions which do not exist.
        self._versions = {}

    def add_version(self, version):
        if not version in self._versions:
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
        Return a list of tags applied to the product. If ``version`` is not ``None``,
        return only those tags which refer to ``version``.
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
        Return the set of all tags which contain a product named ``product_name``.
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
    def __init__(self, pkgroot="https://sw.lsstcorp.org/eupspkg/", pattern=r".*"):
        """
        Only tags which match regular expression ``pattern`` are recorded.
        More tags -> slower loading.
        """
        self._product_tracker = ProductTracker()
        self.pkgroot = pkgroot

        h = html.parse(urlopen(self.pkgroot + "/tags"))
        for el in h.findall("./body/pre/a"):
            if el.text[-5:] == ".list" and re.match(pattern, el.text):
                u = urlopen(pkgroot + '/tags/' + el.get('href'))
                for line in u.read().strip().split('\n'):
                    if "EUPS distribution %s version list" % (el.text[:-5]) in line:
                        continue
                    if line.strip()[0] == "#":
                        continue
                    product, flavor, version = line.split()
                    self._product_tracker.insert(product, version, el.text[:-5])

    def tags_for_product(self, product_name):
        return self._product_tracker.tags_for_product(product_name)

    def products_for_tag(self, tag):
        return self._product_tracker.products_for_tag(tag)


class StackManager(object):
    """
    Convenience class for working with an EUPS product stack installed at ``stack_dir``.
    """
    def __init__(self, stack_dir, pkgroot="http://sw.lsstcorp.org/eupspkg/", debug=True):
        self.stack_dir = stack_dir
        self.flavor = determine_flavor()

        # Generate extra output
        self.debug = debug

        # Generate a minimal working environment for EUPS; best guess without
        # going through setups.sh.
        self.eups_environ = {
            "PATH": "%s:/opt/local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" % (os.path.join(stack_dir, "eups", "bin"),),
            "EUPS_PATH": stack_dir,
            "EUPS_DIR": os.path.join(stack_dir, "eups"),
            "EUPS_SHELL": "sh",
            "PYTHONPATH": os.path.join(stack_dir, "eups", "python"),
            "SETUP_EUPS": "eups LOCAL:%s -f (none) -Z (none)" % (os.path.join(stack_dir, "eups"),),
            "EUPS_PKGROOT": pkgroot
        }

        self._refresh_products()

    def _refresh_products(self):
        self._product_tracker = ProductTracker()

        for line in self._run_cmd("list", "--raw").strip().split('\n'):
            if line == '': continue
            product, version, tags = line.split("|")
            for tag in tags.split(":"):
                if tag in ("setup"):
                    continue
                self._product_tracker.insert(product, version, tag)

        # If a current version of miniconda2 is available, add it to our
        # environment.
        miniconda_version = self._product_tracker.current("miniconda2")
        if miniconda_version:
            miniconda_path = os.path.join(self.stack_dir, self.flavor, "miniconda2", miniconda_version)
            self.eups_environ["PATH"] = "%s:%s" % (os.path.join(miniconda_path, "bin"), self.eups_environ["PATH"])

    def _run_cmd(self, cmd, *args):
        to_exec = ['eups', '--nolocks', cmd]
        to_exec.extend(args)
        if self.debug:
            print self.eups_environ
            print to_exec
        return subprocess.check_output(to_exec, env=self.eups_environ)

    def conda_install(self, package):
        if not self._product_tracker.current("miniconda2"):
            print "Miniconda not available; cannot install %s" % (package,)
            return
        subprocess.check_output(["conda", "install", "--yes", package], env=self.eups_environ)

    def tags_for_product(self, product_name):
        return self._product_tracker.tags_for_product(product_name)

    def version_from_tag(self, product_name, tag):
         for product, version in self._product_tracker.products_for_tag(tag):
            if product == product_name:
                return version

    def distrib_install(self, product_name, version=None, tag=None):
        args = ["install", "--no-server-tags", product_name]
        if version:
            args.append(version)
        if tag:
            args.extend(["-t", tag])
        self._run_cmd("distrib", *args)
        self._refresh_products()

    def add_global_tag(self, tagname):
        startup_path = os.path.join(self.stack_dir, "site", "startup.py")
        with open(startup_path, "a") as startup_py:
            startup_py.write('hooks.config.Eups.globalTags += ["%s"]\n' % (tagname,))

    def apply_tag(self, product_name, version, tagname):
        if self._product_tracker.has_version(product_name, version):
            self._run_cmd("declare", "-t", tagname, product_name, version)
            self._product_tracker.insert(product_name, version, tagname)

    @staticmethod
    def create_stack(stack_dir, clobber=False, pkgroot="http://sw.lsstcorp.org/eupspkg", python="/usr/bin/python", debug=True):
        """
        Check for existence of ``stack_dir``; refuse to proceed if it exists
        and ``clobber`` is ``False``. Otherwise, remove it and start again.

        ``python`` argument is only used for bootstrapping EUPS: we'll install
        Miniconda for working with the stack.
        """
        if clobber:
            shutil.rmtree(stack_dir, ignore_errors=True)
        os.makedirs(stack_dir)

        # Install EUPS into the stack directory.
        EUPS_VERSION = "2.0.1"
        EUPS_URL = "https://github.com/RobertLuptonTheGood/eups/archive/%s.tar.gz" % (EUPS_VERSION,)
        eups_download = urlopen(EUPS_URL)
        tf = tarfile.open(fileobj=eups_download, mode="r|gz")
        eups_build_dir = tempfile.mkdtemp()
        try:
            tf.extractall(eups_build_dir)
            subprocess.check_output(["./configure", "-prefix=%s/eups" % (stack_dir,), "--with-eups=%s" % (stack_dir,), "--with-python=%s" % (python,)], cwd=os.path.join(eups_build_dir, "eups-%s" % (EUPS_VERSION,)))
            subprocess.check_output(["make", "install"], cwd=os.path.join(eups_build_dir, "eups-%s" % (EUPS_VERSION,)))
            if debug:
                print "Done installing EUPS %s" % (EUPS_VERSION,)
        finally:
            shutil.rmtree(eups_build_dir)

        sm = StackManager(stack_dir, pkgroot=pkgroot)
        sm.distrib_install("miniconda2", version=MINICONDA2_VERSION)
        sm.apply_tag("miniconda2", MINICONDA2_VERSION, "current")
        if debug:
            print "Miniconda installed."
        for package in CONDA_PACKAGES:
            sm.conda_install(package)
            if debug:
                print "Conda package %s installed" % (package,)

        sm.distrib_install("lsst")
        return sm


if __name__ == "__main__":
#    sm = StackManager.create_stack("/ssd/swinbank/stacktest")
    rm = RepositoryManager(pattern=r"w_2016_10")
    print rm.products_for_tag("w_2016_10")

    # For each product check if it exists in the stack and apply the tag
    sm = StackManager("/ssd/swinbank/stacktest/")
#    sm.add_global_tag("w_2016_10")
    for product, version in rm.products_for_tag("w_2016_10"):
        sm.apply_tag(product, version, "w_2016_10")
#
#
#
#    for product in PRODUCTS:
#        print "Considering ", product
#        available_tags = rm.tags_for_product(product)
#        installed_tags = sm.installed_tags(product)
#        candidate_tags = available_tags - installed_tags
#        print candidate_tags
#    for product in PRODUCTS:
#        print "Considering ", product
#        available_tags = rm.tags_for_product(product)
#        for tag in available_tags:
#            print "  Installing ", tag
#            sm.distrib_install(product, tag=tag)
#            print "  Adding global tag ", tag
#            sm.add_global_tag(tag)
#            # Get all products + versions tagged with tag on server
#            # Figure out which exist on the client
#            # Apply tag.
#!/usr/bin/python

"""
Generate PKGBUILD file for a Python module from PyPI
"""

# json returns unicode strings
# which causes problems for `dict_get` in python2
from __future__ import unicode_literals

import argparse
from collections import namedtuple
import fileinput
import json
import logging
import os
import re
import sys
import tarfile
import zipfile

IS_PY2 = sys.version_info.major == 2
if IS_PY2:
    from cStringIO import StringIO as BytesIO
    from urllib2 import urlopen, HTTPError
else:
    from io import BytesIO
    from urllib.request import urlopen
    from urllib.error import HTTPError

META = {
    'name': 'pip2pkgbuild',
    'version': '0.5.0',
    'description': 'Generate PKGBUILD file for a Python module from PyPI',
}

logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] : %(message)s'
)
LOG = logging.getLogger('log')

# {{{ Template strings
MODULE_JSON = 'https://pypi.python.org/pypi/{name}/json'
VERSION_MODULE_JSON = 'https://pypi.python.org/pypi/{name}/{version}/json'

MAINTAINER_LINE = '# Maintainer: {name} <{email}>\n'

SPLIT_NAME = """\
pkgbase='{pkgbase}'
pkgname=({pkgname})
"""

SINGLE_NAME = 'pkgname={pkgname}'

HEADERS = """\
_module='{module}'
_src_folder='{src_folder}'
pkgver='{pkgver}'
pkgrel=1
pkgdesc="{pkgdesc}"
url="{url}"
depends=({depends})
makedepends=({mkdepends})
license=('{license}')
arch=('any')
source=("{source}")
sha256sums=('{checksums}')
"""

PREPARE_FUNC = """\
prepare() {
    cp -a "${srcdir}/${_src_folder}"{,-python2}
}
"""

BUILD_FUNC = """\
build() {{
{statements}
}}
"""

BUILD_STATEMENTS = """\
    cd "${{srcdir}}/${{_src_folder}}{suffix}"
    {python} -m build --wheel --no-isolation"""

BUILD_STATEMENTS_OLD = """\
    cd "${{srcdir}}/${{_src_folder}}{suffix}"
    {python} setup.py build"""

# Note: py_pkgname is double-wrapped in braces since the string will be
# formatted twice
INSTALL_LICENSE = (
    '\n'
    'install -D -m644 {license_path}'
    '"${{{{pkgdir}}}}/usr/share/licenses/{{py_pkgname}}/{license_name}"'
)

INSTALL_STATEMENT = """\
    {python} -m installer --destdir="${{pkgdir}}" dist/*.whl"""

INSTALL_STATEMENT_OLD = """\
    {python} setup.py install --root="${{pkgdir}}" --optimize=1 --skip-build"""

SUBPKG_DEPENDS = """
    depends+=({depends})
"""

PACKAGE_FUNC = """\
package{sub_pkgname}() {{{dependencies}
    cd "${{srcdir}}/${{_src_folder}}{suffix}"
{packaging_steps}
}}
"""
# }}}


# {{{ Utils
def known_licenses():
    """
    :rtype: list[str]
    """
    args = {}
    if IS_PY2:
        args['openhook'] = fileinput.hook_encoded('utf-8')
    else:
        args['encoding'] = 'utf-8'
    return fileinput.input(
        files=('/usr/share/licenses/known_spdx_license_identifiers.txt'),
        **args)


def search_in_iter(i, p):
    """Find the first element in an iterable. matching the predicate

    :type i: list[T]
    :type p: (T) -> bool
    :rtype: T
    """
    for x in i:
        if p(x):
            return x
    return None


def search_in_iter_on(proj, i, p):
    """Find the first element in an iterable whose projection satisfies the
    predicate

    :type proj: (U) -> (T)
    :type i: list[U]
    :type p: (T) -> bool
    :rtype: U
    """
    return search_in_iter(map(proj, i), lambda x: p(proj(x)))


def iter_to_str(i):
    """Convert an iterable to a string contained single quoted elements.

    :type i: list
    :rtype: str
    """
    return ' '.join(map("'{}'".format, i))


def dict_get(d, key, default):
    """
    :type d: dict
    :type default T
    :rtype: T
    """
    value = d.get(key)
    return value if isinstance(value, type(default)) else default


def join_nonempty(lines):
    """
    :type lines: list<str>
    :rtype: str
    """
    return '\n'.join([x for x in lines if x])


def removesuffix(s, suffix):
    """
    :type s: str
    :type suffix: str
    :rtype: str
    """
    if s.endswith(suffix):
        return s[:-len(suffix)]
    return s
# }}}


# {{{ fetch_pypi
def get_pypiquery(module, module_version):
    """
    Query parameters for the PyPI lookup

    :type module: str
    :type module_version: num
    """
    return {'name': module, 'version': module_version}


def fetch_pypi(name, version):
    """
    :type name: str
    :type version: str
    :rtype: dict
    """
    def fetch_json(url):
        return json.loads(urlopen(url).read().decode('utf-8'))

    try:
        url = MODULE_JSON.format(name=name)
        info = fetch_json(url)
        if version:
            if info['releases'].get(version) is None:
                raise PythonModuleVersionNotFoundError(
                    '{} {}'.format(name, version))
            url = VERSION_MODULE_JSON.format(name=name, version=version)
            info = fetch_json(url)

    except HTTPError as e:
        if e.code == 404:
            raise PythonModuleNotFoundError('{}'.format(name))
        raise e
    return info


class PythonModuleNotFoundError(Exception):
    """Thrown when the module can't be found on PyPI"""


class PythonModuleVersionNotFoundError(Exception):
    """Thrown when the specified module version can't be found on PyPI"""
# }}}


# {{{ PEP517
def get_pep517(pep517, python):
    """
    Default to PEP517 if python 3 requested or running with a python 3
    interpreter

    Check PEP517 is not requested if a python 2 package (standalone or split)
    is requested

    :type pep517: Bool
    :type python: str
    """

    if pep517 is None:
        if IS_PY2 or python == 'multi' or python == 'python2':
            pep517 = False
        elif not IS_PY2 or python == 'python3':
            pep517 = True

    if pep517 and (
        (python is None and IS_PY2)
        or python == 'multi' or python == 'python2'
    ):
        LOG.error('PEP517 based installation supports Python 3 packages only.')
        sys.exit(1)

    return pep517
# }}}


# {{{ PyModule
def get_licensequery(find_license):
    """Look up license in source archive"""
    return find_license


class PyModule(object):
    """
    Metadata for a python module

    Encapsulates the logic of parsing the PyPI JSON response and elaborating on
    it.
    """

    def __init__(self, json_data, find_license=False):
        """
        :type json_data: dict
        :type find_license: bool
        """
        try:
            info = json_data['info']
            self.module = info['name']
            self.name = self.module.lower()
            self.pkgver = info['version']
            self.pkgdesc = info['summary']
            self.url = info['home_page']
            self.license = self.__get_license(info)
            src_info = self.__get_src_info(json_data['urls'])
            self.source = dict_get(src_info, 'url', '')
            self.checksums = dict_get(
                src_info.get('digests', {}), 'sha256', '')
            self.license_path = None
            if find_license:
                with self.__get_archive(self.source) as file:
                    with Archive.archive_type(self.source)(file) as archive:
                        self.license_path = self.__find_license_path(archive)
        except KeyError as e:
            raise ParseModuleInfoError(e)

    # https://wiki.archlinux.org/index.php/PKGBUILD#license
    @staticmethod
    def __get_license(info):
        """
        :type info: dict
        :rtype: str
        """
        def find_known_licenses(p):
            return search_in_iter_on(
                lambda lic: removesuffix(lic.lower(), ' license'),
                known_licenses(), p)

        lic = find_known_licenses(
            lambda recg: recg == dict_get(info, 'license', ''))

        if lic is None:
            license_str = search_in_iter(
                dict_get(info, 'classifiers', []),
                lambda clsf: clsf.startswith('License'))

            if license_str is None:
                lic = 'unknown'
            else:
                license_str = license_str.split('::')[-1].strip()
                lic = find_known_licenses(
                    lambda recg: recg in license_str)
                if lic is None:
                    lic = 'custom:{}'.format(license_str)
        return lic

    @staticmethod
    def __get_src_info(urls):
        """
        Get supported source url from an iterable of urls

        :type urls: list[dict]
        :rtype: dict
        """
        if len(urls) == 0:
            LOG.warning('Package source not found!')
            LOG.warning('Add it manually and regenerate checksum')
            return {}

        info = search_in_iter(
            urls,
            lambda u: dict_get(u, 'url', '').endswith('.tar.gz'))
        if info is None:
            info = search_in_iter(
                urls,
                lambda u: not dict_get(u, 'url', '').endswith('.whl'))
        if info is None:
            info = urls[0]
        return info

    @staticmethod
    def __get_archive(url):
        """Download the archive of the python module"""

        try:
            return urlopen(url)
        except HTTPError as e:
            LOG.error('Could not retrieve python package for '
                      'license inspection from %s with error %s', url, e)
            return None

    @staticmethod
    def __find_license_path(archive):
        """Determine whether the package source contains a physical license.

        :type archive: Archive
        :rtype: bool|None
        """
        # LICENSE
        # LICENSE.txt
        # license.txt
        # LICENSES.txt
        # license
        if archive is None:
            LOG.warning('Could not find source archive')
            return None

        find_license = re.compile('.*/LICENSES?(?:\\.(txt|rst|md)|)$')

        def match_license(file_path):
            """
            :type file_path: str
            :rtype: str|None
            """
            match = find_license.match(file_path, re.I)
            if match:
                # Remove the subfolder file_path from the match
                # Note: path separators inside a zipfile are always '/'
                return ''.join(match.group(0).split('/')[1:])
            return None

        match = archive.search_compressed_file(match_license)
        if match is None:
            LOG.warning('Could not find license file.')
        return match


class ParseModuleInfoError(Exception):
    """Thrown when the PyPI response is malformed"""
# }}}


# {{{ Archives
class Archive(object):
    """Interface for archive objects (like zip and tar files)"""

    def get_file_listing(self):
        """Return the files present inside of the archive.

        Note tarfile lists the base directory in getnames while
        zipfile does not it its method.

        :rtype: list[str]
        """
        raise NotImplementedError("Archive is an interface")

    def search_compressed_file(self, match):
        """Shallow depth first sarching in compressed file

        :type compressed_source: Archive
        :type match: str -> T|None
        :rtype: T|None
        """
        files = self.get_file_listing()

        def depth(path):
            """Depth of a file path.

            :type path: str
            :rtype: int
            """
            return path.count('/')

        # Prefer matches closer to the root
        sorted_files = sorted(files, key=depth)
        for file_path in sorted_files:
            matched = match(file_path)
            if matched:
                return matched
        return None

    @staticmethod
    def archive_type(url):
        """
        Get the type of the archive returned by url

        :type url: str
        :rtype: type
        """
        if not url:
            LOG.warning('Given url was empty')
            return None
        # Check to see if the file is a tarfile.
        # Unfortunately, splitext only works for files
        # with single extensions
        filename = os.path.basename(url)

        # tar.gz and tar.bz
        if re.match('.*\\.tar\\.(?:gz|bz2)', filename, re.I):
            # The mode needs to be 'r|*', (any type of tarball) which
            # tells tarfile that It should not attempt to
            # seek() or tell() the given
            # object since HTTPResponse doesn't support those operations
            return TarArchive
        # zip
        elif filename.lower().endswith('.zip'):
            return ZipArchive
        else:
            LOG.warning("Source url('%s') "
                        'did not have a zip or tar extension', url)
            return None


class TarArchive(Archive):
    """Tar archive, providing access to its toplevel files"""

    def __init__(self, file):
        self.file = file
        self.archive = None

    def get_file_listing(self):
        return [tar_info.name for
                tar_info in self.archive.getmembers() if not tar_info.isdir()]

    def __enter__(self):
        self.archive = tarfile.open(fileobj=self.file, mode='r|*')
        self.archive.__enter__()
        return self

    def __exit__(self, *args):
        self.archive.__exit__(*args)


class ZipArchive(Archive):
    """Zip archive, providing access to all its files"""

    def __init__(self, file):
        self.file = file
        self.archive = None

    def get_file_listing(self):
        # Remove directories from list
        return [name for
                name in self.archive.namelist() if not name.endswith('/')]

    def __enter__(self):
        self.archive = zipfile.ZipFile(BytesIO(self.file.read()))
        self.archive.__enter__()
        return self

    def __exit__(self, *args):
        self.archive.__exit__(*args)
# }}}


# {{{ SplitMeta
SplitPkg = namedtuple('SplitPkg', ['pkgname', 'depends', 'suffix'])
SplitPkg.__doc__ = """Split PKGBUILD metadata for a single package"""
# Actually, just the split metadata this script cares about


class SplitMeta(object):
    """Collection of the splits in this PKGBUILD"""

    def __init__(self, python, pkgname, py2_pkgname, py3_depends, py2_depends):
        """
        Build the split metadata dict out of the arguments

        If any python's metadata is given, check that python is indeed
        configured (either by -p PYTHON or by -p multi)
        """

        def split(python, name, deps, is_split, is_default):
            return SplitPkg(
                pkgname=name if name is not None else '%s-%s' % (python, name),
                depends=([python] if is_split else []) + ([] if deps is None
                                                          else deps),
                suffix=('-%s' if is_split and not is_default else '')
            )

        self.splits = {}
        if python in {'python', 'multi'}:
            self.splits[python] = split('python',
                                        pkgname,
                                        py3_depends,
                                        python == 'multi',
                                        True)
        if python in {'python2', 'multi'}:
            self.splits[python] = split('python2',
                                        py2_pkgname,
                                        py2_depends,
                                        python == 'multi',
                                        False)

        if (any(v is not None for v in [pkgname, py3_depends]) and
                'python' not in self.splits):
            raise ValueError(
                ('Python 3 package metadata passed: %s\n' +
                 'But requested only Python 2 package to be built!') %
                str({'pkgname': pkgname,
                     'py3_depends': py3_depends}))
        if (any(v is not None for v in [py2_pkgname, py2_depends]) and
                'python2' not in self.splits):
            raise ValueError(
                ('Python 2 package metadata passed: %s\n' +
                 'But requested only Python 3 package to be built!') %
                str({'py2_pkgname': py2_pkgname,
                     'py2_depends': py2_depends}))

    @property
    def is_split(self):
        """Is this package split? Is it configured for multiple pythons?"""

        return len(self.splits) > 1

    @property
    def python_vers(self):
        """For which pythons is this package configured?"""

        return self.splits.keys()

    @property
    def pkgnames(self):
        """Per-python pkgname"""

        return [m.pkgname for m in self.splits.values()]
# }}}


# {{{ Maintainer
Maintainer = namedtuple('Maintainer', ['name', 'email'])
Maintainer.__doc__ = """Representation of maintainer metadata"""


def get_maintainer(name, email):
    """Maintainer line must have either both email and name or neither"""

    if bool(email) != bool(name):
        LOG.error('Must supply either both email and name or neither.')
        sys.exit(1)
    return Maintainer(**locals())
# }}}


# {{{ SharedMeta
class SharedMeta(object):
    """Shared pkgbuild metadata"""

    def __init__(self, mkdepends, backend, depends, pkgbase):
        self.mkdepends = [] if mkdepends is None else mkdepends
        self.depends = [] if depends is None else depends
        self.backend = backend
        self.pkgbase = pkgbase

    def infer(self, splits, pep517):
        """
        Infer the additional metadata

        - pkgbase: if unset, take the unique pkgname if the package is unsplit,
          or the python3 pkgname if the package is split
        - depends: If unsplit, add the python version used. Otherwise, the
          python version will be a per-split dependency
        - mkdepends: Add the backend, and if using PEP517, its infrastructure
          packages
        """

        if not splits.is_split:
            self.depends = splits.python_vers + self.depends

        self.mkdepends = (
            self.__backend_mkdepends(splits, pep517) + self.mkdepends
        )

        self.pkgbase = (
            self.pkgbase if self.pkgbase is not None
            else splits.pkgnames[0] if not splits.is_split
            else splits['python'].pkgname
        )

    def __backend_mkdepends(self, splits, pep517):
        """
        Expand the makedepends given -- get the package corresponding to the
        build backend, list the pep517 packages if requested.

        :param str backend: The build backend used by the module
        """

        modules = [self.backend]
        # Archwiki: [Python_package_guidelines#Standards_based_(PEP_517)]
        if pep517:
            modules += ['build', 'installer', 'wheel']
        return ['%s-%s' % (v, m) for m in modules for v in splits.python_vers]
# }}}


# {{{ Pkgbuild
class Pkgbuild(object):
    """
    Representation of a PKGBUILD

    Encapsulates the metadata-to-PKGBUILD logic
    """

    def __init__(self, module, splitmeta, maintainer=None, sharedmeta=None,
                 pep517=False):
        """
        :type module: PyModule
        :type python: str
        :type splitmeta: SplitMeta
        :type maintainer: Maintainer
        :type sharedmeta: SharedMeta
        :type pep517: Bool
        """
        self.module = module
        self.splitmeta = splitmeta
        self.maintainer = maintainer
        self.sharedmeta = sharedmeta
        self.pep517 = pep517

        self.sharedmeta.infer(self.splitmeta, self.pep517)

    @property
    def is_split(self):
        """Is this package split? Is it configured for multiple pythons?"""

        return self.splitmeta.is_split

    def __steps(self):
        """"
        The parts of the PKGBUILD, as a generator

        This allows avoiding repeated appends when constructing the PKGBUILD.
        """

        if self.maintainer is not None:
            yield MAINTAINER_LINE.format(**self.maintainer._asdict())

        pkg = self.module.source.split('/')[-1]
        src_folder = pkg.split(self.module.pkgver)[0] + self.module.pkgver

        if self.is_split:
            yield SPLIT_NAME.format(
                pkgbase=self.sharedmeta.pkgbase,
                pkgname=iter_to_str(self.splitmeta.pkgnames)
            )
        else:
            yield SINGLE_NAME.format(
                pkgname=iter_to_str(self.splitmeta.pkgnames)
            )

        yield HEADERS.format(
            module=self.module.module,
            src_folder=src_folder,
            pkgver=self.module.pkgver,
            pkgdesc=self.module.pkgdesc,
            url=self.module.url,
            depends=iter_to_str(self.sharedmeta.depends),
            mkdepends=iter_to_str(self.sharedmeta.mkdepends),
            license=self.module.license,
            source=self.module.source,
            checksums=self.module.checksums
        )

        if self.is_split:
            yield PREPARE_FUNC

        build = BUILD_STATEMENTS if self.pep517 else BUILD_STATEMENTS_OLD

        yield BUILD_FUNC.format(statements='\n\n'.join(
            build.format(suffix=meta.suffix, python=py)
            for (py, meta) in self.splitmeta.items())
        )

        install = INSTALL_STATEMENT if self.pep517 else INSTALL_STATEMENT_OLD
        if self.module.license_path:
            license_path = self.module.license_path
            license_command = INSTALL_LICENSE.format(
                license_path=license_path,
                license_name=os.path.basename(license_path)
            )
        else:
            license_command = ''

        for (py, meta) in self.splitmeta.items():
            yield PACKAGE_FUNC.format(
                sub_pkgname=('_'+meta.pkgname) if self.is_split else '',
                dependencies=SUBPKG_DEPENDS.format(
                    depends=iter_to_str(meta.depends)) if meta.depends != []
                else '',
                suffix=meta.suffix,
                packaging_steps=join_nonempty([
                    license_command.format(py_pkgname=meta.pkgname),
                    install.format(python=py)
                ])
            )

    def generate(self):
        """Generate the PKGBUILD functions from the various steps"""
        return '\n'.join(self.__steps())
# }}}


# {{{ parse_args
def parse_args(argv):
    """
    Argument parsing logic

    Separated to clarify the structure of main.
    """

    argparser = argparse.ArgumentParser(prog=META['name'],
                                        description=META['description'])
    argparser.add_argument(
        'module',
        help='The Python module name')
    argparser.add_argument(
        '-v', '--module-version',
        default='',
        help='Use the specified version of the Python module')
    argparser.add_argument(
        '-p', '--python-version',
        choices=['python', 'python2', 'multi'],
        default='python2' if IS_PY2 else 'python',
        dest='python',
        help='The Python version on which the PKGBUILD bases')
    argparser.add_argument(
        '-b', '--package-basename',
        type=str,
        dest='pkgbase',
        help='The value for pkgbase. '
        + 'Default: the first value in pkgname')
    argparser.add_argument(
        '-n', '--package-name',
        type=str,
        dest='pkgname',
        help='The value for pkgname. '
        + 'If the package is split, pkgname of the Python 3 package')
    argparser.add_argument(
        '--python2-package-name',
        type=str,
        dest='py2_pkgname',
        help='The pkgname of the Python 2 package')
    argparser.add_argument(
        '-d', '--depends',
        type=str, default=[], nargs='*',
        help='Dependencies for the whole PKGBUILD')
    argparser.add_argument(
        '--python2-depends',
        dest='py2_depends',
        metavar='DEPENDS',
        type=str, default=None, nargs='*',
        help='Dependencies for the Python 2 package in a split package')
    argparser.add_argument(
        '--python3-depends',
        dest='py3_depends',
        metavar='DEPENDS',
        type=str, default=None, nargs='*',
        help='Dependencies for the Python 3 package in a split package')
    argparser.add_argument(
        '-m', '--make-depends',
        dest='mkdepends',
        type=str, default=[], nargs='*',
        help='Packages to add to makedepends (needed for build only)')
    argparser.add_argument(
        '-s', '--build-backend',
        dest='backend',
        type=str, default='setuptools',
        help='Build backend used by package (default guess: setuptools)')
    argparser.add_argument(
        '-o', '--print-out',
        action='store_true',
        help='Print to stdout rather than saving to PKGBUILD file')
    argparser.add_argument(
        '-V', '--version',
        action='version', version='%(prog)s {}'.format(META['version']))
    argparser.add_argument(
        '-l', '--find-license',
        action='store_true', default=False,
        help='Try to find license file in source files')
    argparser.add_argument(
        '--name', dest='name', default=None,
        help='Name for the package maintainer line')
    argparser.add_argument(
        '--email', dest='email', default=None,
        help='Email for the package maintainer line')
    argparser.add_argument(
        '--pep517', dest='pep517', action='store_true',
        default=None,
        help='Prefer PEP517 based installation method if supported')
    argparser.add_argument(
        '--no-pep517', dest='pep517', action='store_false',
        default=None,
        help='Use old-style installation method unconditionally')

    return argparser.parse_args(argv)


def split_args(args, get):
    """
    Construct the variables defined by `splits` out of `args`

    :type args: argparse.Namespace
    :param function get: A function validating and extracting useful data from
                         the arguments. Its parameters should be named as the
                         arguments to be extracted.
                         __init__ can also be passed, and its `self` argument
                         will be skipped.
    """

    return get(**{key: vars(args)[key] for key in get.__code__.co_varnames
                  if key != 'self'})
# }}}


def get_execoptions(print_out):
    """Options for controlling the program execution"""
    return {'print_out': print_out}


def main(args=sys.argv):
    """The main function"""

    args = parse_args(args[1:])

    pep517 = split_args(args, get_pep517)

    try:
        module = PyModule(fetch_pypi(**split_args(args, get_pypiquery)),
                          split_args(args, get_licensequery))
    except PythonModuleNotFoundError as e:
        LOG.error('Python module not found: %s', e)
        sys.exit(0)
    except PythonModuleVersionNotFoundError as e:
        LOG.error('Python module version not found: %s', e)
        sys.exit(0)
    except ParseModuleInfoError as e:
        LOG.error('Failed to parse Python module information: %s', e)
        sys.exit(0)


    splitmeta = split_args(args, SplitMeta.__init__)
    maintainer = split_args(args, get_maintainer)
    sharedmeta = split_args(args, SharedMeta.__init__)
    pkgbuild = Pkgbuild(module, splitmeta, maintainer,
                        sharedmeta, pep517).generate()

    if args.print_out:
        sys.stdout.write(pkgbuild)
    else:
        with open('PKGBUILD', 'w', encoding='utf-8') as f:
            f.write(pkgbuild)
            LOG.info('Successfully generated PKGBUILD under %s', os.getcwd())


if __name__ == '__main__':
    main()

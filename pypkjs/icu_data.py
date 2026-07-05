"""
Workaround for broken ICU initialisation in STPyV8.

STPyV8 (through at least 13.1.201.22) passes a dangling pointer to
v8::V8::InitializeICUDefaultLocation(): CPlatform::GetICUDataFile() returns
the c_str() of a stack-local std::string, so by the time V8 opens the path
it reads freed memory (in practice an empty string). V8 then runs without
ICU data and aborts the whole process with

    Fatal process out of memory: DateTimePatternGeneratorCache::CreateGenerator

the first time JS touches an Intl-backed API such as
Date.prototype.toLocaleTimeString(). See
https://github.com/cloudflare/stpyv8/issues/125.

When the embedder never hands ICU a data file, ICU falls back to looking for
its data package (icudt<major><endianness>.dat) in the directory named by the
ICU_DATA environment variable. STPyV8 ships the data as
stpyv8-icu/icudtl.dat, so we stage a copy under the name ICU expects and
point ICU_DATA at it.

setup_icu_data() must run before STPyV8 is imported anywhere, because
STPyV8 initialises V8 (and ICU) at import time. On STPyV8 versions where
initialisation works correctly the staged copy is never consulted, so this
is harmless.
"""

import logging
import os
import re
import sys

__author__ = 'katharine'

logger = logging.getLogger('pypkjs.icu_data')

# The ICU package name appears in the data file's table of contents, e.g.
# "icudt74l/". <major> is the ICU major version; the suffix is "l" or "b"
# for little/big endian.
_ICU_NAME_PATTERN = re.compile(rb'icudt\d+[bl]')


def _read_source_data():
    """Return the contents of the icudtl.dat shipped with STPyV8, or None."""
    if sys.version_info < (3, 10):
        try:
            from importlib_resources import files
        except ImportError:
            return None
    else:
        from importlib.resources import files

    try:
        # "stpyv8-icu" is the bare data directory the stpyv8 wheel installs
        # into site-packages; files() resolves it as a namespace portion.
        # STPyV8's own icu_sync() locates it the same way.
        icu_files = files('stpyv8-icu')
    except (ImportError, TypeError, ValueError):
        return None

    for f in icu_files.iterdir():
        if f.name == 'icudtl.dat':
            return f.read_bytes()
    return None


def _data_dir():
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/pypkjs')
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
        if not base:
            return None
        return os.path.join(base, 'pypkjs')
    base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return os.path.join(base, 'pypkjs')


def setup_icu_data():
    """Stage STPyV8's ICU data so ICU's ICU_DATA fallback can find it."""
    if os.environ.get('ICU_DATA'):
        return

    try:
        data = _read_source_data()
        if data is None:
            logger.debug('stpyv8-icu data not found; skipping ICU staging.')
            return

        match = _ICU_NAME_PATTERN.search(data)
        if match is None:
            logger.warning('Could not determine ICU package name from '
                           'icudtl.dat; Intl APIs may crash.')
            return
        name = match.group(0).decode('ascii')

        directory = _data_dir()
        if directory is None:
            return
        target = os.path.join(directory, name + '.dat')

        os.makedirs(directory, exist_ok=True)
        if not os.path.exists(target) or os.path.getsize(target) != len(data):
            tmp = target + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(data)
            os.replace(tmp, target)

        os.environ['ICU_DATA'] = directory
    except Exception:
        logger.warning('Failed to stage ICU data; Intl APIs may crash.',
                       exc_info=True)

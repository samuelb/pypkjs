
__author__ = 'katharine'

from gevent import monkey
monkey.patch_all()

# Must run before STPyV8 is imported: STPyV8 initialises V8 at import time,
# and its own ICU setup is broken (see pypkjs/icu_data.py).
from .icu_data import setup_icu_data
setup_icu_data()

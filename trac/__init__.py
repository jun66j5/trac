# -*- coding: utf-8 -*-
#
# Copyright (C) 2003-2026 Edgewall Software
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at https://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at https://trac.edgewall.org/log/.

__all__ = ['__version__']

try:
    import importlib_metadata as _metadata
except ImportError:
    try:
        import importlib.metadata as _metadata
    except ImportError:
        _metadata = None

if _metadata:
    PackageNotFoundError = _metadata.PackageNotFoundError
    distribution = _metadata.distribution
else:
    from pkg_resources import (
        DistributionNotFound as PackageNotFoundError,
        get_distribution as distribution,
    )

try:
    __version__ = distribution('Trac').version
except PackageNotFoundError:
    __version__ = '1.6.1'

del _metadata, PackageNotFoundError, distribution

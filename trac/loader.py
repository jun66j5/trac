# -*- coding: utf-8 -*-
#
# Copyright (C) 2005-2026 Edgewall Software
# Copyright (C) 2005-2006 Christopher Lenz <cmlenz@gmx.de>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at https://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at https://trac.edgewall.org/log/.
#
# Author: Christopher Lenz <cmlenz@gmx.de>

from glob import glob, escape as glob_escape
import importlib.util
import os.path
import re
import sys
import warnings

from trac.core import ComponentMeta
from trac.util import get_doc, get_module_metadata, get_module_path, \
                      get_pkginfo, get_sources, find_distributions, \
                      parse_version, _metadata
from trac.util.text import exception_to_unicode, to_unicode

__all__ = ['load_components']


def _import_pkg_resources():
    with warnings.catch_warnings():
        # Suppress "pkg_resources is deprecated as an API" warning. The
        # warning is attributed to the importer rather than pkg_resources
        # module, so it is filtered by the message.
        warnings.filterwarnings('ignore',
                                message=r'pkg_resources is deprecated')
        try:
            import pkg_resources
        except ImportError:
            return None
    return pkg_resources


# Prefer pkg_resources if available because importlib.metadata doesn't
# support extracting *.egg files for the plugins
_pkg_resources = _import_pkg_resources()

if _pkg_resources:
    from pkg_resources import working_set, DistributionNotFound, \
                              VersionConflict, UnknownExtra

    _not_found_errors = (DistributionNotFound,)
    _import_errors = (ImportError, UnknownExtra, VersionConflict)
    _version_conflict_errors = (VersionConflict,)
else:
    _not_found_errors = ()
    _import_errors = (ImportError,)
    _version_conflict_errors = ()


def _enable_plugin(env, module):
    """Enable the given plugin module if it wasn't disabled explicitly."""
    if env.is_component_enabled(module) is None:
        env.enable_component(module)


def _log_load_error(env, item, e):
    ue = exception_to_unicode(e)
    if isinstance(e, _not_found_errors):
        env.log.debug('Skipping "%s": %s', item, ue)
    elif isinstance(e, _import_errors):
        env.log.error('Skipping "%s": %s', item, ue)
    else:
        env.log.error('Skipping "%s": %s', item,
                      exception_to_unicode(e, traceback=True))


def _deregister_components(module_name, attrs):
    """Remove components for the entry point from the registry."""
    for name in attrs:
        for c in ComponentMeta._components:
            if c.__module__ == module_name and c.__name__ == name:
                ComponentMeta.deregister(c)


def load_eggs(entry_point_name):
    """Loader that loads any eggs on the search path and `sys.path`."""
    def _load_eggs(env, search_path, auto_enable=None):
        if auto_enable:
            auto_enable = os.path.normcase(auto_enable)
        if _pkg_resources:
            _load_eggs_pkg_resources(env, entry_point_name, search_path,
                                     auto_enable)
        else:
            _load_eggs_metadata(env, entry_point_name, search_path,
                                auto_enable)
    return _load_eggs


def _load_eggs_pkg_resources(env, entry_point_name, search_path,
                             auto_enable):
    # Note that the following doesn't seem to support unicode search_path
    distributions, errors = working_set.find_plugins(
        _pkg_resources.Environment(search_path)
    )
    for dist in distributions:
        if dist not in working_set:
            env.log.debug('Adding plugin "%s" from "%s"',
                          dist, dist.location)
            working_set.add(dist)

    for dist, e in errors.items():
        _log_load_error(env, dist, e)

    for entry in sorted(working_set.iter_entry_points(entry_point_name),
                        key=lambda entry: entry.name):
        env.log.debug('Loading plugin "%s" from "%s"',
                      entry.name, entry.dist.location)
        try:
            entry.load(require=True)
        except Exception as e:
            _log_load_error(env, entry, e)
            _deregister_components(entry.module_name, entry.attrs)
        else:
            if os.path.normcase(os.path.dirname(entry.dist.location)) == \
                    auto_enable:
                _enable_plugin(env, entry.module_name)


def _load_eggs_metadata(env, entry_point_name, search_path, auto_enable):
    for entry, dist in _find_plugins(env, search_path):
        if entry not in sys.path:
            env.log.debug('Adding plugin "%s %s" from "%s"',
                          dist.name, dist.version, entry)
            sys.path.insert(0, entry)

    for entry in sorted(_metadata.entry_points(group=entry_point_name),
                        key=lambda entry: entry.name):
        location = _dist_location(entry.dist)
        env.log.debug('Loading plugin "%s" from "%s"', entry.name, location)
        try:
            entry.load()
        except Exception as e:
            _log_load_error(env, entry, e)
            _deregister_components(entry.module,
                                   entry.attr.split('.') if entry.attr
                                   else ())
        else:
            if os.path.normcase(os.path.dirname(location)) == auto_enable:
                _enable_plugin(env, entry.module)


def _find_plugins(env, search_path):
    """Yield `(path_entry, distribution)` tuples for the newest version of
    each distribution found in the eggs and the directories on the search
    path.
    """
    def version_key(dist):
        try:
            return parse_version(dist.version)
        except Exception:
            return parse_version('0')

    candidates = {}
    for path in search_path:
        entries = sorted(glob(os.path.join(glob_escape(path), '*.egg')))
        entries.append(path)
        for entry in entries:
            for dist in _metadata.distributions(path=[entry]):
                name = _normalize_name(dist.name)
                if name in candidates and \
                        version_key(candidates[name][1]) >= version_key(dist):
                    continue
                candidates[name] = (entry, dist)

    for name, (entry, dist) in sorted(candidates.items()):
        try:
            installed = _metadata.distribution(dist.name)
        except _metadata.PackageNotFoundError:
            pass
        else:
            location = _dist_location(installed)
            if os.path.normcase(location) != \
                    os.path.normcase(_dist_location(dist)):
                env.log.debug('Skipping "%s %s" from "%s": "%s %s" is '
                              'already installed in "%s"', dist.name,
                              dist.version, entry, installed.name,
                              installed.version, location)
                continue
        yield entry, dist


def _normalize_name(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def _safe_name(name):
    """Convert an arbitrary string to a standard distribution name, same
    as `pkg_resources.safe_name()`.
    """
    return re.sub(r'[^A-Za-z0-9.]+', '-', name)


def _dist_location(dist):
    """Return the base location of the `importlib.metadata` distribution,
    e.g. `site-packages` directory or `*.egg` path.
    """
    return os.path.normpath(str(dist.locate_file('')))


def _dist_info(dist):
    """Return `(name, version, location)` of the distribution provided by
    either `importlib.metadata` or `pkg_resources`.
    """
    if hasattr(dist, 'project_name'):  # pkg_resources
        return dist.project_name, dist.version, dist.location
    return dist.metadata['Name'], dist.version, _dist_location(dist)


def _read_metadata_text(dist, name):
    """Return the contents of the metadata file of the distribution
    provided by either `importlib.metadata` or `pkg_resources`, or `None`
    if the file is not found.
    """
    if hasattr(dist, 'read_text'):  # importlib.metadata
        return dist.read_text(name)
    try:
        return dist.get_metadata(name)
    except (KeyError, OSError):
        return None


def load_py_files():
    """Loader that look for Python source files in the plugins directories,
    which simply get imported, thereby registering them with the component
    manager if they define any components.
    """

    def load_source(name, filename):
        spec = importlib.util.spec_from_file_location(name, filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
        return module

    def _load_py_files(env, search_path, auto_enable=None):
        for path in search_path:
            plugin_files = glob(os.path.join(path, '*.py'))
            for plugin_file in plugin_files:
                plugin_name = os.path.basename(plugin_file[:-3])
                env.log.debug("Loading file plugin %s from %s",
                              plugin_name, plugin_file)
                try:
                    if plugin_name not in sys.modules:
                        load_source(plugin_name, plugin_file)
                except (ImportError,) + _version_conflict_errors as e:
                    env.log.error('Skipping "%s": %s', plugin_name,
                                  exception_to_unicode(e))
                except (Exception, SystemExit) as e:
                    env.log.error(
                        "Failed to load plugin from %s: %s", plugin_file,
                        exception_to_unicode(e, traceback=True))
                else:
                    if path == auto_enable:
                        _enable_plugin(env, plugin_name)

    return _load_py_files


def load_components(env, extra_path=None, loaders=(load_eggs('trac.plugins'),
                                                   load_py_files())):
    """Load all plugin components found on the given search path."""
    plugins_dir = env.plugins_dir
    search_path = [plugins_dir]
    if extra_path:
        search_path += list(extra_path)

    for loadfunc in loaders:
        loadfunc(env, search_path, auto_enable=plugins_dir)


def get_plugin_info(env, include_core=False):
    """Return package information about Trac core and installed plugins."""
    path_sources = {}

    def find_distribution(module):
        name = module.__name__
        path = get_module_path(module)
        sources = path_sources.get(path)
        if sources is None:
            sources = path_sources[path] = get_sources(path)
        dist = sources.get(name.replace('.', '/') + '.py')
        if dist is None:
            dist = sources.get(name.replace('.', '/') + '/__init__.py')
        return dist

    plugins_dir = env.plugins_dir
    plugins = {}
    for component in ComponentMeta._components:
        module = sys.modules[component.__module__]

        dist = find_distribution(module)
        if dist is not None:
            name, version, location = _dist_info(dist)
        else:
            # This is a plain Python source file, not an egg
            name, version, location = _safe_name(module.__name__), '', \
                                      module.__file__
        plugin_filename = None
        if os.path.normcase(os.path.realpath(os.path.dirname(location))) \
                == plugins_dir:
            plugin_filename = os.path.basename(location)

        if name not in plugins:
            readonly = True
            if plugin_filename and os.access(location,
                                             os.F_OK + os.W_OK):
                readonly = False
            # retrieve plugin metadata
            info = get_pkginfo(dist) if dist is not None else {}
            if info:
                # Info found; set all those fields to "None" that have the
                # value "UNKNOWN" as this is the value for fields that
                # aren't specified in "setup.py"
                for k in info:
                    if info[k] == 'UNKNOWN':
                        info[k] = ''
                    else:
                        # Must be encoded as unicode as otherwise Genshi
                        # may raise a "UnicodeDecodeError".
                        info[k] = to_unicode(info[k])
            else:
                info = get_module_metadata(module)
                version = info['version']

            plugins[name] = {
                'name': name, 'version': version,
                'path': location, 'plugin_filename': plugin_filename,
                'readonly': readonly, 'info': info, 'modules': {},
            }
        modules = plugins[name]['modules']
        if module.__name__ not in modules:
            summary, description = get_doc(module)
            plugins[name]['modules'][module.__name__] = {
                'summary': summary, 'description': description,
                'components': {},
            }
        full_name = module.__name__ + '.' + component.__name__
        summary, description = get_doc(component)
        c = component
        if c in env and not issubclass(c, env.__class__):
            c = component(env)
        modules[module.__name__]['components'][component.__name__] = {
            'full_name': full_name,
            'summary': summary, 'description': description,
            'enabled': env.is_component_enabled(component),
            'required': getattr(c, 'required', False),
        }
    if not include_core:
        for name in list(plugins):
            if name.lower() == 'trac':
                plugins.pop(name)
    return sorted(iter(plugins.values()),
                  key=lambda p: (p['name'].lower() != 'trac',
                                 p['name'].lower()))


def match_plugins_to_frames(plugins, frames):
    """Add a `frame_idx` element to plugin information as returned by
    `get_plugin_info()`, containing the index of the highest frame in the
    list that was located in the plugin.
    """
    egg_frames = [(i, f) for i, f in enumerate(frames)
                  if f['filename'].startswith('build/')]

    def find_egg_frame_index(plugin):
        for dist in find_distributions(plugin['path']):
            sources = _read_metadata_text(dist, 'SOURCES.txt')
            if sources is None:
                continue    # Metadata not found
            for src in sources.splitlines():
                if src.endswith('.py'):
                    nsrc = src.replace('\\', '/')
                    for i, f in egg_frames:
                        if f['filename'].endswith(nsrc):
                            plugin['frame_idx'] = i
                            return

    for plugin in plugins:
        base, ext = os.path.splitext(plugin['path'].replace('\\', '/'))
        if ext == '.egg' and egg_frames:
            find_egg_frame_index(plugin)
        else:
            for i, f in enumerate(frames):
                if f['filename'].startswith(base):
                    plugin['frame_idx'] = i
                    break

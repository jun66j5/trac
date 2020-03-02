# -*- coding: utf-8 -*-
#
# Copyright (C) 2008-2020 Edgewall Software
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at https://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at https://trac.edgewall.org/log/.

"""better_twill is a small wrapper around twill to set some sane defaults and
monkey-patch some better versions of some of twill's methods.
It also handles twill's absense.
"""

import io
import re
import os
import sys
import tempfile
from os.path import abspath, dirname, join
from pkg_resources import parse_version as pv
from urllib.parse import urljoin
from urllib.request import HTTPBasicAuthHandler, Request, build_opener, \
                           pathname2url

from trac.test import mkdtemp, rmtree

# On OSX lxml needs to be imported before twill to avoid Resolver issues
# somehow caused by the mac specific 'ic' module
try:
    from lxml import etree
except ImportError:
    etree = None

try:
    import selenium
except ImportError:
    selenium = None


if selenium:
    from selenium import webdriver
    from selenium.common.exceptions import (
        NoSuchElementException, WebDriverException as ConnectError)
    from selenium.webdriver.common.action_chains import ActionChains

    # setup short names to reduce typing
    # This selenium browser (and the tc commands that use it) are essentially
    # global, and not tied to our test fixture.

    class Proxy(object):

        driver = None
        tmpdir = None
        auth_handler = None

        def init(self):
            self.tmpdir = mkdtemp()
            self.driver = self._create_webdriver()
            self.driver.maximize_window()

        def _create_webdriver(self):
            if os.name == 'posix':
                mime_types = os.path.join(tempfile.mkdtemp(dir=self.tmpdir),
                                          'mime.types')
                with open(mime_types, 'w', encoding='utf-8') as f:
                    f.write('multipart/related mht\n')
            else:
                mime_types = None
            profile = webdriver.FirefoxProfile()
            profile.set_preference('intl.accept_languages', 'en-us')
            profile.set_preference('network.http.phishy-userpass-length', 255)
            profile.set_preference('general.warnOnAboutConfig', False)
            if mime_types:
                profile.set_preference('helpers.private_mime_types_file',
                                       mime_types)
            options = webdriver.FirefoxOptions()
            options.profile = profile
            options.add_argument('--headless')
            options.log.level = 'debug'
            log_path = 'geckodriver.log'
            open(log_path, 'w').close()
            return webdriver.Firefox(options=options,
                                     service_log_path=log_path)

        def close(self):
            if self.tmpdir:
                rmtree(self.tmpdir)
                self.tmpdir = None
            if self.driver:
                self.driver.quit()
                self.driver = None

        def go(self, url):
            url = self._urljoin(url)
            return self.driver.get(url)

        def back(self):
            self.driver.back()

        def click(self, *args, **kwargs):
            self._find_by(*args, **kwargs).click()

        def reload(self):
            self.driver.refresh()

        def download(self, url):
            cookie = '; '.join('%s=%s' % (c['name'], c['value'])
                               for c in self.driver.get_cookies())
            url = self._urljoin(url)
            handlers = []
            if self.auth_handler:
                handlers.append(self.auth_handler)
            opener = build_opener(*handlers)
            req = Request(url, headers={'Cookie': cookie})
            with opener.open(req) as resp:
                return resp.getcode(), resp.read()

        _normurl_re = re.compile(r'[a-z]+://[^:/]+:?[0-9]*$')

        def url(self, url, regexp=True):
            current_url = self.get_url()
            if regexp:
                if not re.match(url, current_url):
                    raise AssertionError("URL didn't match: {!r} not matched "
                                         "in {!r}".format(url, current_url))
            else:
                if self._normurl_re.match(url):
                    url += '/'
                if url != current_url:
                    raise AssertionError("URL didn't equal: {!r} != {!r}"
                                         .format(url, current_url))

        def notfind(self, s, flags=None):
            source = self.get_source()
            match = re.search(s, source, self._re_flags(flags))
            if match:
                url = self.write_source(source)
                raise AssertionError('Regex matched: {!r} matches {!r} in {}'
                                     .format(source[match.start():match.end()],
                                             s, url))

        def find(self, s, flags=None):
            source = self.get_source()
            if not re.search(s, source, self._re_flags(flags)):
                url = self.write_source(source)
                raise AssertionError("Regex didn't match: {!r} not found in {}"
                                     .format(s, url))

        def add_auth(self, x, url, username, password):
            handler = HTTPBasicAuthHandler()
            handler.add_password(x, url, username, password)
            self.auth_handler = handler

        def follow(self, s):
            self._find_link(s).click()

        def download_link(self, pattern):
            element = self._find_link(pattern)
            href = element.get_attribute('href')
            return self.download(href)

        def formvalue(self, form, field, value):
            form_element = self._find_by(id=form)
            elements = form_element.find_elements_by_css_selector(
                                    '[name="{0}"], [id="{0}"]'.format(field))
            for element in elements:
                tag = element.tag_name
                if tag == 'input':
                    type_ = element.get_attribute('type')
                    if type_  in ('text', 'password', 'file'):
                        element.clear()
                        element.send_keys(value)
                        return
                    if type_ == 'checkbox':
                        if isinstance(value, str):
                            v = value[1:] \
                                if value.startswith(('+', '-')) else value
                            if element.get_attribute('value') != v:
                                continue
                            checked = not value.startswith('-')
                        elif value in (True, False):
                            checked = value
                        else:
                            raise ValueError('Unrecognized value for '
                                             'checkbox: %s' % repr(value))
                        element.click()  # to focus
                        if element.is_selected() != checked:
                            element.click()
                        return
                    if type_ == 'radio':
                        if element.get_attribute('value') == value:
                            element.click()
                            return
                if tag == 'textarea':
                    element.clear()
                    element.send_keys(value)
                    return
                if tag == 'select':
                    for option in element.find_elements_by_tag_name('option'):
                        if value == option.get_attribute('value') or \
                                value == option.get_property('textContent'):
                            option.click()
                            element.click()  # to focus the select element
                            return
                    else:
                        url = self.write_source()
                        raise ValueError('Missing option[value=%r] in %s' %
                                         (value, url))
            else:
                url = self.write_source()
                raise ValueError('Unable to find element matched with '
                                 '`formvalue(%r, %r, %r)` in %s' %
                                 (form, field, value, url))

        fv = formvalue

        def formfile(self, formname, fieldname, filename, content_type=None,
                     fp=None):
            if fp:
                phypath = os.path.join(tempfile.mkdtemp(dir=self.tmpdir),
                                       filename)
                with open(phypath, 'wb') as f:
                    f.write(fp.read())
            else:
                phypath = os.path.abspath(filename)

            form = self._find_form(formname)
            enctype = form.get_attribute('enctype')
            if enctype != 'multipart/form-data':
                url = self.write_source()
                raise ValueError('enctype should be multipart/form-data: %r '
                                 'in %s' % (enctype, url))
            field = self._find_field(fieldname, formname)
            type_ = field.get_attribute('type')
            if type_ != 'file':
                url = self.write_source()
                raise ValueError('type should be file: %r in %s' %
                                 (type_, url))
            field.send_keys(phypath)

        def javascript_disabled(self, fn):
            def wrapper(*args, **kwargs):
                prev = self.set_prefs({'javascript.enabled': False})
                try:
                    return fn(*args, **kwargs)
                finally:
                    if prev is not None:
                        self.set_prefs(prev)
            return wrapper

        def prefs(self, values):
            def decorator(fn):
                def wrapper(*args, **kwargs):
                    prev = self.set_prefs(values)
                    try:
                        return fn(*args, **kwargs)
                    finally:
                        if prev is not None:
                            self.set_prefs(prev)
                return wrapper
            return decorator

        def set_prefs(self, values):
            driver = self.driver
            driver.get('about:config')
            prev = driver.execute_script("""\
                var prefs = Components.classes
                            ["@mozilla.org/preferences-service;1"]
                            .getService(Components.interfaces.nsIPrefBranch);
                var values = arguments[0];
                var prev = {};
                var key, value;
                for (key in values) {
                    switch (prefs.getPrefType(key)) {
                    case 32:
                        value = prefs.getCharPref(key);
                        break;
                    case 64:
                        value = prefs.getIntPref(key);
                        break;
                    case 128:
                        value = prefs.getBoolPref(key);
                        break;
                    default:
                        continue;
                    }
                    prev[key] = value;
                }
                for (key in values) {
                    value = values[key];
                    switch (typeof value) {
                    case 'string':
                        prefs.setCharPref(key, value);
                        break;
                    case 'number':
                        prefs.setIntPref(key, value);
                        break;
                    case 'boolean':
                        prefs.setBoolPref(key, value);
                        break;
                    }
                }
                return prev;
            """, values)
            return prev

        def submit(self, fieldname=None, formname=None):
            element = self._find_field(fieldname, formname)
            if element.get_attribute('type') == 'submit':
                if not element.is_enabled():
                    raise ValueError('Unable to click disabled submit element')
            else:
                if element.tag_name != 'form':
                    element = element.get_property('form')
                    if element is None:
                        url = self.write_source()
                        raise ValueError('No form property in %s' % url)
                for element in element.find_elements_by_css_selector(
                        '[type="submit"]'):
                    if element.is_enabled():
                        break
                else:
                    url = self.write_source()
                    raise ValueError('No active submit elements in %s' % url)
            element.click()

        def move_to(self, *args, **kwargs):
            element = self._find_by(*args, **kwargs)
            ActionChains(self.driver).move_to_element(element).perform()

        def _find_form(self, id_):
            selector = 'form[id="%(name)s"]' % {'name': id_}
            return self._find_by(selector)

        def _find_field(self, field=None, form=None):
            if field is form is None:
                return self.driver.switch_to.active_element
            node = self.driver
            try:
                if form:
                    node = node.find_element_by_css_selector(
                        'form[id="{0}"], form[name="{0}"]'.format(form))
                if field:
                    node = node.find_element_by_css_selector(
                        '[id="{0}"], [name="{0}"], '
                        '[type="submit"][value="{0}"]'.format(field))
                return node
            except NoSuchElementException as e:
                url = self.write_source()
                raise AssertionError('Missing field (%r, %r) in %s' %
                                     (field, form, url)) from e

        def _find_by(self, *args, **kwargs):
            driver = self.driver
            try:
                if kwargs.get('id'):
                    return driver.find_element_by_id(kwargs['id'])
                if kwargs.get('name'):
                    return driver.find_element_by_name(kwargs['name'])
                if kwargs.get('class_'):
                    return driver.find_element_by_class_name(kwargs['class_'])
                if len(args) == 1:
                    return driver.find_element_by_css_selector(args[0])
                if len(args) == 0:
                    return driver.switch_to.active_element
            except NoSuchElementException as e:
                url = self.write_source()
                raise AssertionError('Missing element (%r, %r) in %s' %
                                     (args, kwargs, url)) from e
            raise ValueError('Invalid arguments: %r %r' % (args, kwargs))

        def _find_link(self, pattern):
            re_pattern = re.compile(pattern)
            search = lambda text: text and re_pattern.search(text)
            for element in self.driver.find_elements_by_tag_name('a'):
                if search(element.get_property('textContent')) or \
                        search(element.get_attribute('href')):
                    return element
            else:
                url = self.write_source(self.get_source())
                raise AssertionError('Missing link %r in %s' % (pattern, url))

        _re_flag_bits = {'i': re.IGNORECASE, 'm': re.MULTILINE, 's': re.DOTALL}

        def _re_flags(self, flags):
            bit = 0
            if flags is not None:
                for flag in flags:
                    try:
                        value = self._re_flag_bits[flag]
                    except IndexError:
                        raise ValueError('Invalid flags %r' % flags)
                    else:
                        bit |= value
            return bit

        def _urljoin(self, url):
            if '://' not in url:
                url = urljoin(self.get_url(), url)
            return url

        # When we can't find something we expected, or find something we didn't
        # expect, it helps the debugging effort to have a copy of the html to
        # analyze.
        def write_source(self, source=None):
            """Write the current html to a file. Name the file based on the
            current testcase.
            """
            import unittest

            frame = sys._getframe()
            while frame:
                if frame.f_code.co_name in ('runTest', 'setUp', 'tearDown'):
                    testcase = frame.f_locals['self']
                    testname = testcase.__class__.__name__
                    tracdir = testcase._testenv.tracdir
                    break
                elif isinstance(frame.f_locals.get('self'), unittest.TestCase):
                    testcase = frame.f_locals['self']
                    testname = '%s.%s' % (testcase.__class__.__name__,
                                          testcase._testMethodName)
                    tracdir = testcase._testenv.tracdir
                    break
                frame = frame.f_back
            else:
                # We didn't find a testcase in the stack, so we have no clue
                # what's going on.
                raise Exception("No testcase was found on the stack. This was "
                                "really not expected, and I don't know how to "
                                "handle it.")

            if source is None:
                source = self.get_source()
            filename = os.path.join(tracdir, 'log', '%s.html' % testname)
            try:
                if isinstance(source, bytes):
                    html_file = open(filename, 'wb')
                else:
                    html_file = open(filename, 'w', encoding='utf-8')
                html_file.write(source)
            finally:
                html_file.close()

            return urljoin('file:', pathname2url(filename))

        def get_url(self):
            return self.driver.current_url

        def get_source(self):
            return self.driver.page_source

        get_html = get_source

else:
    class ConnectError(Exception): pass

    class Proxy(object):

        def javascript_disabled(self, fn):
            def wrapper(*args, **kwargs):
                return fn(*args, **kwargs)
            return wrapper

        def prefs(self, values):
            def decorator(fn):
                def wrapper(*args, **kwargs):
                    return fn(*args, **kwargs)
                return wrapper
            return decorator


b = tc = Proxy()


if b is not None and False: # TODO selenium
    # Setup XHTML validation for all retrieved pages
    try:
        from lxml import etree
    except ImportError:
        print("SKIP: validation of XHTML output in functional tests"
              " (no lxml installed)")
        etree = None

    if etree and pv(etree.__version__) < pv('2.0.0'):
        # 2.0.7 and 2.1.x are known to work.
        print("SKIP: validation of XHTML output in functional tests"
              " (lxml < 2.0, api incompatibility)")
        etree = None

    if etree:
        class _Resolver(etree.Resolver):
            base_dir = dirname(abspath(__file__))

            def resolve(self, system_url, public_id, context):
                return self.resolve_filename(join(self.base_dir,
                                                  system_url.split("/")[-1]),
                                             context)

        _parser = etree.XMLParser(dtd_validation=True)
        _parser.resolvers.add(_Resolver())
        etree.set_default_parser(_parser)

        def _format_error_log(data, log):
            msg = []
            for entry in log:
                context = data.splitlines()[max(0, entry.line - 5):
                                            entry.line + 6]
                msg.append("\n# %s\n# URL: %s\n# Line %d, column %d\n\n%s\n"
                           % (entry.message, entry.filename, entry.line,
                              entry.column, "\n".join(each.decode('utf-8')
                                                      for each in context)))
            return "\n".join(msg).encode('ascii', 'xmlcharrefreplace')

        def _validate_xhtml(func_name, *args, **kwargs):
            page = b.get_html()
            if "xhtml1-strict.dtd" not in page:
                return
            etree.clear_error_log()
            try:
                # lxml will try to convert the URL to unicode by itself,
                # this won't work for non-ascii URLs, so help him
                url = b.get_url()
                if isinstance(url, bytes):
                    url = str(url, 'latin1')
                etree.parse(io.BytesIO(page), base_url=url)
            except etree.XMLSyntaxError as e:
                raise twill.errors.TwillAssertionError(
                    _format_error_log(page, e.error_log))

        b._post_load_hooks.append(_validate_xhtml)

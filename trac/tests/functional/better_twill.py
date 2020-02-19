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
from urllib.request import pathname2url

from trac.test import mkdtemp, rmtree
from trac.util.text import to_unicode

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
    from selenium.common.exceptions import WebDriverException as ConnectError
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.remote import file_detector

    # setup short names to reduce typing
    # This selenium browser (and the tc commands that use it) are essentially
    # global, and not tied to our test fixture.

    class Proxy(object):

        driver = None
        tmpdir = None

        def __init__(self):
            self.tmpdir = mkdtemp()
            self.driver = self._create_webdriver()

        def _create_webdriver(self):
            profile = webdriver.FirefoxProfile()
            profile.set_preference('network.http.phishy-userpass-length', 255)
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
            if self.driver:
                self.driver.quit()

        def go(self, url):
            return self.driver.get(url)

        def back(self):
            self.driver.back()

        def url(self, url):
            if not re.match(url, self.driver.current_url):
                raise AssertionError("URL didn't match: {!r} not matched in "
                                     "{!r}".format(url, self.get_url()))
        def notfind(self, s):
            source = self.get_source()
            match = re.search(s, source)
            if match:
                url = self._write_source(source)
                raise AssertionError('Regex matched: {!r} matches {!r} in {}'
                                     .format(source[match.start():match.end()],
                                             s, url))
        def find(self, s):
            source = self.get_source()
            if not re.search(s, source):
                url = self._write_source(source)
                raise AssertionError("Regex didn't match: {!r} not found in {}"
                                     .format(s, url))

        def add_auth(self, x, url, username, pw):
            pass

        def follow(self, s):
            search = re.compile(s).search
            for element in self.driver.find_elements_by_tag_name('a'):
                text = element.get_property('textContent')
                if search(text):
                    element.click()
                    break
            else:
                url = self._write_source(self.get_source())
                raise AssertionError('Unable to find link %r in %s' % (s, url))

        def formvalue(self, form, field, value):
            form_element = self._find_by(id=form)
            elements = form_element.find_elements_by_name(field)
            if not elements:
                raise ValueError('Missing %s in form#%s' % (field, form))
            element = elements[0]
            tag = element.tag_name
            if tag == 'input':
                type_ = element.get_attribute('type')
                if type_  in ('text', 'password', 'file'):
                    element.clear()
                    element.send_keys(value)
                elif type_ == 'checkbox':
                    if element.is_selected() != bool(value):
                        element.click()
                elif type_ == 'radio':
                    for element in elements:
                        if element.get_attribute('value') == value:
                            element.click()
                            break
                    else:
                        raise ValueError('Missing input[type=%r][value=%r]' %
                                         (type_, value))
                else:
                    raise ValueError('Unrecognized element: input[type=%r]' %
                                     type_)
            elif tag == 'textarea':
                element.clear()
                element.send_keys(value)
            elif tag == 'select':
                for option in element.find_elements_by_tag_name('option'):
                    v = option.get_attribute('value') or \
                            option.get_property('textContent')
                    if v == value:
                        option.click()
                        break
                    else:
                        raise ValueError('Missing option[value=%r]' % value)
            else:
                raise ValueError('Unrecognized element: %r' % tag)

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
                raise ValueError('ERROR: enctype should be '
                                 'multipart/form-data: %r' % enctype)
            field = self._find_field(fieldname, formname)
            type_ = field.get_attribute('type')
            if type_ != 'file':
                raise ValueError('ERROR: type should be file: %r' % type_)
            field.send_keys(phypath)

        def submit(self, fieldname=None, formname=None):
            element = self._find_field(fieldname, formname)
            if element.get_attribute('type') != 'submit':
                if element.tag_name != 'form':
                    element = element.get_property('form')
                for element in element.find_elements_by_css_selector(
                        '[type="submit"]'):
                    if element.is_enabled():
                        break
                else:
                    raise ValueError('No active submit elements')
            element.click()

        def move_to(self, *args, **kwargs):
            element = self._find_by(*args, **kwargs)
            ActionChains(self.driver).move_to_element(element).perform()

        def _find_form(self, id_):
            selector = 'form[id="%(name)s"]' % {'name': id_}
            return self._find_by(selector)

        def _find_field(self, fieldname=None, formname=None):
            if fieldname and formname:
                selector = 'form[id="%(form)s"] [id="%(field)s"], ' \
                           'form[id="%(form)s"] [name="%(field)s"]' % \
                           {'form': formname, 'field': fieldname}
            elif fieldname:
                selector = '[id="%(field)s"], [name="%(field)s"]' % \
                           {'field': fieldname}
            elif formname:
                selector = 'form[id="%s"] [type="submit"]' % formname
            else:
                return self.driver.switch_to.active_element
            return self._find_by(selector)

        def _find_by(self, *args, **kwargs):
            driver = self.driver
            if kwargs.get('id'):
                return driver.find_element_by_id(kwargs.get('id'))
            if kwargs.get('name'):
                return driver.find_element_by_name(kwargs.get('name'))
            if kwargs.get('class_'):
                return driver.find_element_by_class_name(kwargs.get('class_'))
            if len(args) == 1:
                return driver.find_element_by_css_selector(args[0])
            if len(args) == 0:
                return driver.switch_to.active_element
            raise ValueError('Invalid arguments: %r %r' % (args, kwargs))

        # When we can't find something we expected, or find something we didn't
        # expect, it helps the debugging effort to have a copy of the html to
        # analyze.
        def _write_source(self, source):
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

            filename = os.path.join(tracdir, 'log', '%s.html' % testname)
            with open(filename, 'w', encoding='utf-8') as html_file:
                html_file.write(source)

            return urljoin('file:', pathname2url(filename))

        def get_url(self):
            return self.driver.current_url

        def get_source(self):
            return self.driver.page_source

        get_html = get_source

    import atexit
    tc = Proxy()
    atexit.register(tc.close)
    b = tc
else:
    class ConnectError(Exception): pass
    b = tc = None

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

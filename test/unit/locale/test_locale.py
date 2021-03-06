#!/usr/bin/env python
#-*- coding:utf-8 -*-

import os
import unittest
import string
import sys
import threading

try:
    from subprocess import check_output
except ImportError:
    from subprocess import Popen, PIPE, CalledProcessError

    def check_output(*popenargs, **kwargs):
        """Lifted from python 2.7 stdlib."""
        if 'stdout' in kwargs:
            raise ValueError('stdout argument not allowed, it will be '
                             'overridden.')
        process = Popen(stdout=PIPE, *popenargs, **kwargs)
        output, unused_err = process.communicate()
        retcode = process.poll()
        if retcode:
            cmd = kwargs.get("args")
            if cmd is None:
                cmd = popenargs[0]
            raise CalledProcessError(retcode, cmd, output=output)
        return output


class TestTranslations(unittest.TestCase):

    def setUp(self):
        self.la = os.environ.get('LC_ALL')
        self.sl = os.environ.get('SWIFT_LOCALEDIR')
        os.environ['LC_ALL'] = 'eo'
        os.environ['SWIFT_LOCALEDIR'] = os.path.dirname(__file__)
        self.orig_stop = threading._DummyThread._Thread__stop
        # See http://stackoverflow.com/questions/13193278/\
        #     understand-python-threading-bug
        threading._DummyThread._Thread__stop = lambda x: 42

    def tearDown(self):
        if self.la is not None:
            os.environ['LC_ALL'] = self.la
        else:
            del os.environ['LC_ALL']
        if self.sl is not None:
            os.environ['SWIFT_LOCALEDIR'] = self.sl
        else:
            del os.environ['SWIFT_LOCALEDIR']
        threading._DummyThread._Thread__stop = self.orig_stop

    def test_translations(self):
        path = ':'.join(sys.path)
        translated_message = check_output(['python', __file__, path])
        self.assertEquals(translated_message, 'testo mesaĝon\n')


if __name__ == "__main__":
    os.environ['LC_ALL'] = 'eo'
    os.environ['SWIFT_LOCALEDIR'] = os.path.dirname(__file__)
    sys.path = string.split(sys.argv[1], ':')
    from swift import gettext_ as _
    print _('test message')

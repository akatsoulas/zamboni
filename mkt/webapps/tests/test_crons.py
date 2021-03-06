# -*- coding: utf-8 -*-
import os
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.core.files.storage import default_storage as storage
from django.core.management import call_command

import mock
from nose.tools import eq_

import mkt
import mkt.site.tests
from mkt.api.models import Nonce
from mkt.developers.models import ActivityLog
from mkt.files.models import File, FileUpload
from mkt.site.fixtures import fixture
from mkt.users.models import UserProfile
from mkt.versions.models import Version
from mkt.webapps import cron
from mkt.webapps.cron import clean_old_signed, mkt_gc, update_downloads
from mkt.webapps.models import Webapp


class TestLastUpdated(mkt.site.tests.TestCase):
    fixtures = fixture('webapp_337141')

    def test_catchall(self):
        """Make sure the catch-all last_updated is stable and accurate."""
        # Nullify all datestatuschanged so the public add-ons hit the
        # catch-all.
        (File.objects.filter(status=mkt.STATUS_PUBLIC)
         .update(datestatuschanged=None))
        Webapp.objects.update(last_updated=None)

        cron.addon_last_updated()
        for addon in Webapp.objects.filter(status=mkt.STATUS_PUBLIC):
            eq_(addon.last_updated, addon.created)

        # Make sure it's stable.
        cron.addon_last_updated()
        for addon in Webapp.objects.filter(status=mkt.STATUS_PUBLIC):
            eq_(addon.last_updated, addon.created)


class TestHideDisabledFiles(mkt.site.tests.TestCase):
    fixtures = fixture('webapp_337141')

    msg = 'Moving disabled file: %s => %s'

    def setUp(self):
        self.addon = Webapp.objects.get(pk=337141)
        self.version = self.addon.latest_version
        self.f1 = self.version.all_files[0]

    @mock.patch('mkt.files.models.os')
    def test_leave_nondisabled_files(self, os_mock):
        stati = [(mkt.STATUS_PUBLIC, mkt.STATUS_PUBLIC)]
        for addon_status, file_status in stati:
            self.addon.update(status=addon_status)
            File.objects.update(status=file_status)
            cron.hide_disabled_files()
            assert not os_mock.path.exists.called, (addon_status, file_status)

    @mock.patch('mkt.files.models.File.mv')
    @mock.patch('mkt.files.models.storage')
    def test_move_user_disabled_addon(self, m_storage, mv_mock):
        # Use Webapp.objects.update so the signal handler isn't called.
        Webapp.objects.filter(id=self.addon.id).update(
            status=mkt.STATUS_PUBLIC, disabled_by_user=True)
        File.objects.update(status=mkt.STATUS_PUBLIC)
        cron.hide_disabled_files()

        # Check that f1 was moved.
        mv_mock.assert_called_with(self.f1.file_path,
                                   self.f1.guarded_file_path, self.msg)
        # There's only 1 file.
        eq_(mv_mock.call_count, 1)

    @mock.patch('mkt.files.models.File.mv')
    @mock.patch('mkt.files.models.storage')
    def test_move_admin_disabled_addon(self, m_storage, mv_mock):
        Webapp.objects.filter(id=self.addon.id).update(
            status=mkt.STATUS_DISABLED)
        File.objects.update(status=mkt.STATUS_PUBLIC)
        cron.hide_disabled_files()
        # Check that f1 was moved.
        mv_mock.assert_called_with(self.f1.file_path,
                                   self.f1.guarded_file_path, self.msg)
        # There's only 1 file.
        eq_(mv_mock.call_count, 1)

    @mock.patch('mkt.files.models.File.mv')
    @mock.patch('mkt.files.models.storage')
    def test_move_disabled_file(self, m_storage, mv_mock):
        Webapp.objects.filter(id=self.addon.id).update(
            status=mkt.STATUS_REJECTED)
        File.objects.filter(id=self.f1.id).update(status=mkt.STATUS_DISABLED)
        cron.hide_disabled_files()
        # f1 should have been moved.
        mv_mock.assert_called_with(self.f1.file_path,
                                   self.f1.guarded_file_path, self.msg)
        eq_(mv_mock.call_count, 1)

    @mock.patch('mkt.files.models.File.mv')
    @mock.patch('mkt.files.models.storage')
    def test_ignore_deleted_versions(self, m_storage, mv_mock):
        # Apps only have 1 file and version delete only deletes one.
        self.version.delete()
        mv_mock.reset_mock()
        # Create a new version/file just like the one we deleted.
        version = Version.objects.create(addon=self.addon)
        File.objects.create(version=version, filename='f2')
        cron.hide_disabled_files()
        # Mock shouldn't have been called.
        assert not mv_mock.called, mv_mock.call_args


class TestWeeklyDownloads(mkt.site.tests.TestCase):

    def setUp(self):
        self.app = Webapp.objects.create(status=mkt.STATUS_PUBLIC)

    def get_app(self):
        return Webapp.objects.get(pk=self.app.pk)

    @mock.patch('mkt.webapps.tasks.get_monolith_client')
    def test_weekly_downloads(self, _mock):
        client = mock.Mock()
        raw = {
            'facets': {
                'installs': {
                    '_type': 'date_histogram',
                    'entries': [
                        {'count': 3,
                         'time': 1390780800000,
                         'total': 19.0},
                        {'count': 62,
                         'time': 1391385600000,
                         'total': 236.0}
                    ]
                }
            }
        }
        client.raw.return_value = raw
        _mock.return_value = client

        eq_(self.app.weekly_downloads, 0)

        update_downloads([self.app.pk])

        self.app.reload()
        eq_(self.app.weekly_downloads, 255)

    @mock.patch('mkt.webapps.tasks.get_monolith_client')
    def test_total_downloads(self, _mock):
        client = mock.Mock()
        raw = {
            'facets': {
                'installs': {
                    u'_type': u'statistical',
                    u'count': 49,
                    u'total': 6638.0
                }
            }
        }
        client.raw.return_value = raw
        _mock.return_value = client

        eq_(self.app.total_downloads, 0)

        update_downloads([self.app.pk])

        self.app.reload()
        eq_(self.app.total_downloads, 6638)

    @mock.patch('mkt.webapps.tasks.get_monolith_client')
    def test_monolith_error(self, _mock):
        client = mock.Mock()
        client.side_effect = ValueError
        client.raw.side_effect = Exception
        _mock.return_value = client

        update_downloads([self.app.pk])

        self.app.reload()
        eq_(self.app.weekly_downloads, 0)
        eq_(self.app.total_downloads, 0)


class TestCleanup(mkt.site.tests.TestCase):

    def setUp(self):
        self.file = os.path.join(settings.SIGNED_APPS_REVIEWER_PATH,
                                 '1', 'x.z')

    def test_not_cleaned(self):
        storage.open(self.file, 'w')
        clean_old_signed()
        assert storage.exists(self.file)

    def test_cleaned(self):
        storage.open(self.file, 'w')
        clean_old_signed(-60)
        assert not storage.exists(self.file)


@mock.patch('lib.crypto.packaged.sign_app')
class TestSignApps(mkt.site.tests.TestCase):
    fixtures = fixture('webapp_337141')

    def setUp(self):
        self.app = Webapp.objects.get(id=337141)
        self.app.update(is_packaged=True)
        self.app2 = mkt.site.tests.app_factory(
            name=u'Mozillaball ょ', app_slug='test',
            is_packaged=True, version_kw={'version': '1.0',
                                          'created': None})
        self.app3 = mkt.site.tests.app_factory(
            name='Test app 3', app_slug='test3', status=mkt.STATUS_REJECTED,
            is_packaged=True, version_kw={'version': '1.0',
                                          'created': None})

    def test_by_webapp(self, sign_mock):
        v1 = self.app.current_version
        call_command('sign_apps', webapps=str(v1.pk))
        file1 = v1.all_files[0]
        assert sign_mock.called_with(((file1.file_path,
                                       file1.signed_file_path),))

    def test_all(self, sign_mock):
        v1 = self.app.current_version
        v2 = self.app2.current_version
        call_command('sign_apps')
        file1 = v1.all_files[0]
        file2 = v2.all_files[0]
        eq_(len(sign_mock.mock_calls), 2)
        eq_(sign_mock.mock_calls[0][1][:2],
            (file1.file_path, file1.signed_file_path))
        eq_(sign_mock.mock_calls[1][1][:2],
            (file2.file_path, file2.signed_file_path))


@mock.patch('os.stat')
@mock.patch('os.listdir')
@mock.patch('os.remove')
class TestGarbage(mkt.site.tests.TestCase):

    def setUp(self):
        self.user = UserProfile.objects.create(
            email='gc_test@example.com', name='gc_test')
        mkt.log(mkt.LOG.CUSTOM_TEXT, 'testing', user=self.user,
                created=datetime(2001, 1, 1))

    def test_garbage_collection(self, rm_mock, ls_mock, stat_mock):
        eq_(ActivityLog.objects.all().count(), 1)
        mkt_gc()
        eq_(ActivityLog.objects.all().count(), 0)

    def test_nonce(self, rm_mock, ls_mock, stat_mock):
        nonce = Nonce.objects.create(nonce='a', timestamp=1, client_key='b')
        nonce.update(created=self.days_ago(2))
        eq_(Nonce.objects.count(), 1)
        mkt_gc()
        eq_(Nonce.objects.count(), 0)

    def test_dump_delete(self, rm_mock, ls_mock, stat_mock):
        ls_mock.return_value = ['lol']
        stat_mock.return_value = StatMock(days_ago=1000)

        mkt_gc()
        assert rm_mock.call_args_list[0][0][0].endswith('lol')

    def test_new_no_delete(self, rm_mock, ls_mock, stat_mock):
        ls_mock.return_value = ['lol']
        stat_mock.return_value = StatMock(days_ago=1)

        mkt_gc()
        assert not rm_mock.called

    def test_old_and_new(self, rm_mock, ls_mock, stat_mock):
        fu_new = FileUpload.objects.create(path='/tmp/bar', name='bar')
        fu_new.created = self.days_ago(5)
        fu_old = FileUpload.objects.create(path='/tmp/foo', name='foo')
        fu_old.update(created=self.days_ago(91))

        mkt_gc()

        eq_(FileUpload.objects.count(), 1)
        assert rm_mock.called
        eq_(rm_mock.call_args[0][0], fu_old.path)

    def test_old_no_path(self, rm_mock, ls_mock, stat_mock):
        fu_old = FileUpload.objects.create(path='', name='foo')
        fu_old.update(created=self.days_ago(91))

        mkt_gc()

        eq_(FileUpload.objects.count(), 0)
        assert not rm_mock.called


class StatMock(object):
    def __init__(self, days_ago):
        self.st_mtime = time.mktime(
            (datetime.now() - timedelta(days_ago)).timetuple())
        self.st_size = 100

# Copyright 2021 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import threading

from lxml import etree
from nova.tests.functional import integrated_helpers
from nova.tests.functional.libvirt import base as libvirt_base


class LiveMigrationQueuedAbortTest(
    libvirt_base.LibvirtMigrationMixin,
    libvirt_base.ServersTestBase,
    integrated_helpers.InstanceHelperMixin
):
    """Functional test for bug 1949808.

    This test is used to confirm that VM's state is reverted properly
    when queued Live migration is aborted.
    """

    api_major_version = 'v2.1'
    microversion = '2.74'
    ADMIN_API = True

    def setUp(self):
        super().setUp()

        # We will allow only one live migration to be processed at any
        # given period of time
        self.flags(max_concurrent_live_migrations='1')
        self.src_hostname = self.start_compute(hostname='src')
        self.dest_hostname = self.start_compute(hostname='dest')

        self.src = self.computes[self.src_hostname]
        self.dest = self.computes[self.dest_hostname]

        # Live migration's execution could be locked if needed
        self.lock_live_migration = threading.Lock()

    def _migrate_stub(self, domain, destination, params, flags):
        # Execute only if live migration is not locked
        with self.lock_live_migration:
            self.dest.driver._host.get_connection().createXML(
                params['destination_xml'],
                'fake-createXML-doesnt-care-about-flags')
            conn = self.src.driver._host.get_connection()

            # Because migrateToURI3 is spawned in a background thread,
            # this method does not block the upper nova layers. Because
            # we don't want nova to think the live migration has
            # finished until this method is done, the last thing we do
            # is make fakelibvirt's Domain.jobStats() return
            # VIR_DOMAIN_JOB_COMPLETED.
            server = etree.fromstring(
                params['destination_xml']
            ).find('./uuid').text
            dom = conn.lookupByUUIDString(server)
            dom.complete_job()

    def test_queued_live_migration_abort(self):
        # Lock live migrations
        self.lock_live_migration.acquire()

        # Start instances: first one would be used to occupy
        # executor's live migration queue, second one would be used
        # to actually confirm that queued live migrations are
        # aborted properly.
        self.server_a = self._create_server(
            host=self.src_hostname, networks='none')
        self.server_b = self._create_server(
            host=self.src_hostname, networks='none')
        # Issue live migration requests for both servers. We expect that
        # server_a live migration would be running, but locked by
        # self.lock_live_migration and server_b live migration would be
        # queued.
        self._live_migrate(
            self.server_a,
            migration_expected_state='running',
            server_expected_state='MIGRATING'
        )
        self._live_migrate(
            self.server_b,
            migration_expected_state='queued',
            server_expected_state='MIGRATING'
        )

        # Abort live migration for server_b
        serverb_migration = self.api.api_get(
            '/os-migrations?instance_uuid=%s' % self.server_b['id']
        ).body['migrations'].pop()

        self.api.api_delete(
            '/servers/%s/migrations/%s' % (self.server_b['id'],
                                           serverb_migration['id']))
        self._wait_for_migration_status(self.server_b, ['cancelled'])
        # Unlock live migrations and confirm that server_a becomes
        # active again after successful live migration
        self.lock_live_migration.release()
        self._wait_for_state_change(self.server_a, 'ACTIVE')

        # FIXME(artom) Assert the server_b never comes out of 'MIGRATING'
        self.assertRaises(
            AssertionError,
            self._wait_for_state_change, self.server_b, 'ACTIVE')
        self._wait_for_state_change(self.server_b, 'MIGRATING')

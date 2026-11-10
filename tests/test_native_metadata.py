"""Ownership and cancellation checks without launching an EDA process."""
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from caplab import native_metadata as metadata


class MetadataWorkerTests(unittest.TestCase):
    def make_worker(self, folder):
        with mock.patch.object(metadata, 'ROOT', Path(folder)):
            return metadata.MetadataWorker(folder)

    def test_mutable_or_simulation_commands_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            worker=self.make_worker(folder)
            for body in ('dbSave(cv)', 'dbCreateInst(cv m "I0" list(0 0) "R0")', 'maeRunSimulation()', 'asiSetDesign(ts "x")'):
                with self.assertRaisesRegex(ValueError,'read-only'):
                    worker.request(body,folder,1,None,None)
            self.assertIsNone(worker.process)

    def test_cancelled_operation_does_not_launch_a_process(self):
        with tempfile.TemporaryDirectory() as folder:
            worker=self.make_worker(folder);cancelled=threading.Event();cancelled.set()
            with mock.patch.object(worker,'_start') as start:
                with self.assertRaisesRegex(RuntimeError,'Stopped'):
                    worker.request('ddGetLibList()',folder,1,cancelled,None)
                start.assert_not_called()

    def test_waiting_cancel_does_not_stop_another_request(self):
        with tempfile.TemporaryDirectory() as folder:
            worker=self.make_worker(folder);cancelled=threading.Event();cancelled.set()
            worker._serial.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError,'Stopped'):
                    worker.request('ddGetLibList()',folder,1,cancelled,None)
                self.assertFalse(worker._closed.is_set())
            finally:worker._serial.release()

    def test_inherited_worker_cannot_kill_parent_process(self):
        with tempfile.TemporaryDirectory() as folder:
            worker=self.make_worker(folder);worker.owner=os.getpid()+100
            worker.process=mock.Mock()
            with mock.patch.object(metadata.os,'killpg') as kill:
                worker.close();kill.assert_not_called()

    def test_exited_child_and_changed_group_are_not_killed(self):
        with tempfile.TemporaryDirectory() as folder:
            worker=self.make_worker(folder);worker.process=mock.Mock(pid=12345)
            worker.process.poll.return_value=0
            with mock.patch.object(metadata.os,'killpg') as kill:
                worker.close();kill.assert_not_called()
            worker=self.make_worker(folder);worker.process=mock.Mock(pid=12345)
            worker.process.poll.return_value=None
            with mock.patch.object(metadata.os,'getpgid',return_value=6789), mock.patch.object(metadata.os,'killpg') as kill:
                worker.close();kill.assert_not_called()


if __name__=='__main__':unittest.main()

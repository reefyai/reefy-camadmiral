import json
import subprocess
import unittest
from unittest.mock import patch
from scripts.ci_image_retry import pull_image, prefetch_compose_images, transient_download_failure


class ImageRetryTests(unittest.TestCase):
    def result(self, code=0, detail=''):
        return subprocess.CompletedProcess(['synthetic-docker'], code, '', detail)

    @patch('scripts.ci_image_retry.time.sleep')
    @patch('scripts.ci_image_retry.subprocess.run')
    def test_tls_transfer_is_retried_but_success_returns_once(self, run, sleep):
        run.side_effect=[self.result(1,'local error: tls: bad record MAC'),self.result()]
        pull_image(['synthetic-docker','--host','unix:///synthetic-job.sock'],'synthetic/image:1')
        self.assertEqual(run.call_count,2);sleep.assert_called_once_with(5)
        self.assertTrue(all(c.kwargs['timeout']==90 for c in run.call_args_list))

    @patch('scripts.ci_image_retry.time.sleep')
    @patch('scripts.ci_image_retry.subprocess.run')
    def test_transport_exhaustion_is_bounded(self, run, sleep):
        run.return_value=self.result(1,'unexpected EOF')
        with self.assertRaises(subprocess.CalledProcessError):pull_image(['synthetic-docker'],'synthetic/image:1')
        self.assertEqual(run.call_count,6)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[5,10,15,20,25])

    @patch('scripts.ci_image_retry.subprocess.run')
    def test_access_and_trust_failures_are_never_retried(self, run):
        for detail in ['unauthorized','manifest unknown','x509: certificate signed by unknown authority',
                       'certificate verify failed','no space left','denied: forbidden',
                       'bad record MAC and certificate verification failed']:
            run.reset_mock();run.return_value=self.result(1,detail)
            with self.assertRaises(subprocess.CalledProcessError):pull_image(['synthetic-docker'],'synthetic/image:1')
            self.assertEqual(run.call_count,1,detail)

    @patch('scripts.ci_image_retry.time.sleep')
    @patch('scripts.ci_image_retry.subprocess.run')
    def test_timeouts_have_the_same_six_attempt_bound(self, run, sleep):
        run.side_effect=subprocess.TimeoutExpired(['synthetic-docker'],90)
        with self.assertRaises(subprocess.TimeoutExpired):pull_image(['synthetic-docker'],'synthetic/image:1')
        self.assertEqual(run.call_count,6)

    @patch('scripts.ci_image_retry.subprocess.run')
    def test_non_start_commands_are_not_replayed_or_prefetched(self, run):
        for command in [['docker','exec','synthetic','test'],['docker','compose','-p','synthetic','logs'],
                        ['docker','compose','-p','synthetic','run','--rm','driver'],
                        ['docker','compose','-f','synthetic.yml','run','driver','up']]:
            prefetch_compose_images(command)
        run.assert_not_called()

    @patch('scripts.ci_image_retry.pull_image')
    @patch('scripts.ci_image_retry.subprocess.run')
    def test_prefetch_uses_the_same_private_route_and_selected_dependencies(self, run, pull):
        services={'application':{'image':'synthetic/local:1','depends_on':{'database':{}}},
                  'database':{'image':'synthetic/database:1'},
                  'unused':{'image':'synthetic/unused:1'}}
        run.side_effect=[subprocess.CompletedProcess([],0,json.dumps({'services':services}),''),
                         self.result(0),self.result(1)]
        route=['/synthetic/docker','--config','/synthetic/config','--host','unix:///synthetic-job.sock']
        prefetch_compose_images([*route,'compose','-p','synthetic','-f','synthetic.yml','up','-d','application'])
        pull.assert_called_once_with(route,'synthetic/local:1')
        self.assertEqual(run.call_count,3)
        self.assertTrue(all(c.args[0][:len(route)]==route for c in run.call_args_list))

    @patch('scripts.ci_image_retry.pull_image')
    @patch('scripts.ci_image_retry.subprocess.run')
    def test_existing_verified_images_are_not_downloaded_again(self, run, pull):
        run.side_effect=[subprocess.CompletedProcess([],0,json.dumps({'services':{'application':{'image':'synthetic/local:1'}}}),''),self.result()]
        prefetch_compose_images(['docker','compose','up','-d','application'])
        pull.assert_not_called()

    def test_classifier_does_not_retry_unknown_command_failures(self):
        self.assertFalse(transient_download_failure('AssertionError: invalid recording'))
        self.assertTrue(transient_download_failure('unexpected media type text/html'))


if __name__=='__main__':unittest.main()

import os
import tempfile
import unittest
from uuid import uuid4

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'programs' / 'blockchain_votingsystem.py'
os.environ['FLASK_SECRET_KEY'] = 'test-only-session-secret'
os.environ['ADMIN_USERNAME'] = 'test-admin'
os.environ['ADMIN_PASSWORD'] = 'test-only-password'
os.environ['VOTELEDGER_DATABASE'] = str(Path(tempfile.gettempdir()) / f'blockchain-voting-tests-{uuid4().hex}.db')

spec = importlib.util.spec_from_file_location('blockchain_votingsystem', MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BlockchainVotingAppTests(unittest.TestCase):
    def setUp(self):
        self.client = module.app.test_client()

    def test_root_page_loads(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Blockchain Voting System', response.get_data(as_text=True))

    def test_admin_login_rejects_unconfigured_credentials(self):
        original_username = module.ADMIN_USERNAME
        original_password = module.ADMIN_PASSWORD
        module.ADMIN_USERNAME = ''
        module.ADMIN_PASSWORD = ''
        try:
            response = self.client.post('/admin/login', json={'username': '', 'password': ''})
        finally:
            module.ADMIN_USERNAME = original_username
            module.ADMIN_PASSWORD = original_password
        self.assertEqual(response.status_code, 401)

    def test_login_and_otp_verification(self):
        voter_id = f'TEST_LOGIN_{uuid4().hex[:8]}'
        phone_number = f'98765{uuid4().int % 100000:05d}'
        self.client.post('/admin/login', json={'username': module.ADMIN_USERNAME, 'password': module.ADMIN_PASSWORD})
        registration_response = self.client.post('/admin/voters', json={
            'name': 'Alice Example',
            'dob': '1995-05-01',
            'phone_number': phone_number,
            'state': 'California',
            'voter_id': voter_id
        })
        self.assertEqual(registration_response.status_code, 201)
        login_response = self.client.post('/login', json={
            'name': 'Alice Example',
            'dob': '1995-05-01',
            'phone_number': phone_number,
            'voter_id': voter_id,
            'state': 'California'
        })
        self.assertEqual(login_response.status_code, 200)
        payload = login_response.get_json()
        self.assertIn('OTP', payload['message'])

        normalized = module.normalize_phone(phone_number)
        self.assertEqual(normalized, f'+91{phone_number}')

        otp = payload['demo_otp']
        verify_response = self.client.post('/verify_otp', json={
            'phone_number': normalized,
            'otp': otp
        })
        self.assertEqual(verify_response.status_code, 200)
        self.assertIn('verified', verify_response.get_json()['message'].lower())

        first_election_name = f'Voting Election {uuid4().hex[:8]}'
        first_election_response = self.client.post('/admin/elections', json={
            'name': first_election_name, 'description': 'Voting test election',
            'start_date': '2026-01-01', 'end_date': '2026-12-31', 'active': True
        })
        self.assertEqual(first_election_response.status_code, 201)
        first_election = next(item for item in self.client.get('/admin/data').get_json()['elections'] if item['name'] == first_election_name)
        first_candidate_response = self.client.post('/admin/candidates', json={'election_id': first_election['id'], 'name': 'First Candidate', 'party': 'Test Party'})
        self.assertEqual(first_candidate_response.status_code, 201)
        first_candidate = next(item for item in self.client.get('/admin/data').get_json()['candidates'] if item['election_id'] == first_election['id'])
        first_vote = self.client.post('/vote', json={'election_id': first_election['id'], 'candidate_id': first_candidate['id']})
        self.assertEqual(first_vote.status_code, 201)

        second_election_name = f'Second Election {uuid4().hex[:6]}'
        second_election = self.client.post('/admin/elections', json={
            'name': second_election_name, 'description': 'Duplicate vote test',
            'start_date': '2026-01-01', 'end_date': '2026-12-31', 'active': True
        })
        self.assertEqual(second_election.status_code, 201)
        admin_data = self.client.get('/admin/data').get_json()
        second_id = next(item['id'] for item in admin_data['elections'] if item['name'] == second_election_name)
        candidate_response = self.client.post('/admin/candidates', json={'election_id': second_id, 'name': 'Second Candidate', 'party': 'Test Party'})
        self.assertEqual(candidate_response.status_code, 201)
        second_candidate = next(item for item in self.client.get('/admin/data').get_json()['candidates'] if item['election_id'] == second_id)
        second_vote = self.client.post('/vote', json={'election_id': second_id, 'candidate_id': second_candidate['id']})
        self.assertEqual(second_vote.status_code, 409)

    def test_admin_can_delete_voter_and_update_candidate_details(self):
        self.client.post('/admin/login', json={'username': module.ADMIN_USERNAME, 'password': module.ADMIN_PASSWORD})

        voter_id = f'REMOVE_TEST_{uuid4().hex[:8]}'
        phone_number = f'98765{uuid4().int % 100000:05d}'
        create_voter = self.client.post('/admin/voters', json={
            'name': 'Delete Me',
            'dob': '1998-02-15',
            'phone_number': phone_number,
            'state': 'Tamil Nadu',
            'voter_id': voter_id
        })
        self.assertEqual(create_voter.status_code, 201)
        voter = next(item for item in self.client.get('/admin/data').get_json()['voters'] if item['voter_id'] == voter_id)

        delete_response = self.client.delete(f"/admin/voters/{voter['id']}")
        self.assertEqual(delete_response.status_code, 200)
        self.assertNotIn(voter_id, [item['voter_id'] for item in self.client.get('/admin/data').get_json()['voters']])

        election_name = f'Candidate Edit Election {uuid4().hex[:6]}'
        election_response = self.client.post('/admin/elections', json={
            'name': election_name,
            'description': 'Candidate edit test',
            'start_date': '2026-01-01',
            'end_date': '2026-12-31',
            'active': True
        })
        self.assertEqual(election_response.status_code, 201)
        election_record = next(item for item in self.client.get('/admin/data').get_json()['elections'] if item['name'] == election_name)

        candidate_response = self.client.post('/admin/candidates', json={
            'election_id': election_record['id'],
            'name': 'Old Candidate',
            'party': 'Old Party'
        })
        self.assertEqual(candidate_response.status_code, 201)
        candidate = next(item for item in self.client.get('/admin/data').get_json()['candidates'] if item['election_id'] == election_record['id'])

        update_response = self.client.put(f"/admin/candidates/{candidate['id']}", json={
            'name': 'Updated Candidate',
            'party': 'Updated Party'
        })
        self.assertEqual(update_response.status_code, 200)
        updated = next(item for item in self.client.get('/admin/data').get_json()['candidates'] if item['id'] == candidate['id'])
        self.assertEqual(updated['name'], 'Updated Candidate')
        self.assertEqual(updated['party'], 'Updated Party')

    def test_admin_views_separate_election_and_candidate_management(self):
        self.client.post('/admin/login', json={'username': module.ADMIN_USERNAME, 'password': module.ADMIN_PASSWORD})

        election_view = self.client.get('/admin/elections')
        self.assertEqual(election_view.status_code, 200)
        election_html = election_view.get_data(as_text=True)
        self.assertIn('id="elections"', election_html)
        self.assertIn('Create an election', election_html)

        candidate_view = self.client.get('/admin/candidates')
        self.assertEqual(candidate_view.status_code, 200)
        candidate_html = candidate_view.get_data(as_text=True)
        self.assertIn('id="candidates"', candidate_html)
        self.assertIn('Add a candidate', candidate_html)

    def test_default_admin_nav_keeps_core_sections_and_management_links(self):
        self.client.post('/admin/login', json={'username': module.ADMIN_USERNAME, 'password': module.ADMIN_PASSWORD})
        page = self.client.get('/admin')
        page_html = page.get_data(as_text=True)

        for label in ['Overview', 'Voter registry', 'Vote activity', 'Blockchain', 'Election management', 'Candidate management']:
            self.assertIn(label, page_html)

    def test_public_login_hides_temporary_otp_details(self):
        page = self.client.get('/')
        page_text = page.get_data(as_text=True)
        self.assertNotIn('Development delivery code', page_text)
        self.assertNotIn('Code sent to', page_text)

    def test_unregistered_voter_id_is_rejected(self):
        response = self.client.post('/login', json={
            'name': 'Alice',
            'dob': '1995-05-01',
            'phone_number': '9347796080',
            'voter_id': 'UNKNOWN_VOTER',
            'state': 'Andhra Pradesh'
        })
        self.assertEqual(response.status_code, 404)

    def test_public_registration_and_transaction_hash_are_restricted(self):
        registration_response = self.client.post('/register', json={
            'name': 'Fake User', 'dob': '1990-01-01', 'phone_number': '9876543210', 'state': 'Delhi'
        })
        self.assertNotEqual(registration_response.status_code, 201)

        voter_response = self.client.post('/vote', json={'election_id': 1, 'candidate_id': 1})
        self.assertEqual(voter_response.status_code, 403)

    def test_admin_can_delete_voter(self):
        voter_id = f'DELETE_TEST_{uuid4().hex[:8]}'
        phone_number = f'98765{uuid4().int % 100000:05d}'
        self.client.post('/admin/login', json={'username': module.ADMIN_USERNAME, 'password': module.ADMIN_PASSWORD})
        create_response = self.client.post('/admin/voters', json={
            'name': 'Delete User',
            'dob': '1990-01-01',
            'phone_number': phone_number,
            'state': 'Delhi',
            'voter_id': voter_id
        })
        self.assertEqual(create_response.status_code, 201)
        created_voter = create_response.get_json()
        delete_response = self.client.delete(f"/admin/voters/{created_voter['id']}")
        self.assertEqual(delete_response.status_code, 200)
        self.assertIn('removed', delete_response.get_json()['message'].lower())

    def test_admin_can_remove_candidate_without_votes(self):
        self.client.post('/admin/login', json={'username': module.ADMIN_USERNAME, 'password': module.ADMIN_PASSWORD})
        election_response = self.client.post('/admin/elections', json={
            'name': f'Candidate Removal {uuid4().hex[:8]}',
            'description': 'Candidate removal test',
            'start_date': '2026-01-01',
            'end_date': '2026-12-31',
            'active': False
        })
        self.assertEqual(election_response.status_code, 201)
        election_id = next(item['id'] for item in self.client.get('/admin/data').get_json()['elections'] if item['name'].startswith('Candidate Removal '))
        candidate_response = self.client.post('/admin/candidates', json={
            'election_id': election_id,
            'name': 'Removable Candidate',
            'party': 'Removable Party'
        })
        self.assertEqual(candidate_response.status_code, 201)
        candidate = next(item for item in self.client.get('/admin/data').get_json()['candidates'] if item['name'] == 'Removable Candidate')
        delete_response = self.client.delete(f"/admin/candidates/{candidate['id']}")
        self.assertEqual(delete_response.status_code, 200)
        self.assertIn('party removed', delete_response.get_json()['message'].lower())

    def test_admin_login_and_csv_export(self):
        login_response = self.client.post('/admin/login', json={
            'username': module.ADMIN_USERNAME,
            'password': module.ADMIN_PASSWORD
        })
        self.assertEqual(login_response.status_code, 200)

        export_response = self.client.get('/admin/export/csv')
        self.assertEqual(export_response.status_code, 200)
        csv_data = export_response.get_data(as_text=True)
        self.assertIn('name,dob', csv_data)
        self.assertIn('state', csv_data)

        first_election = self.client.get('/admin/data').get_json()['elections'][0]
        close_response = self.client.post(f"/admin/elections/{first_election['id']}/status", json={'active': False})
        self.assertEqual(close_response.status_code, 200)

        new_name = f'Next Election {uuid4().hex[:8]}'
        create_response = self.client.post('/admin/elections', json={
            'name': new_name, 'description': 'Replacement election',
            'start_date': '2026-09-06', 'end_date': '2026-12-31', 'active': True
        })
        self.assertEqual(create_response.status_code, 201)
        elections = self.client.get('/admin/data').get_json()['elections']
        active_elections = [election for election in elections if election['active']]
        self.assertEqual(len(active_elections), 1)
        self.assertEqual(active_elections[0]['name'], new_name)

        clear_response = self.client.post('/admin/elections/clear', json={'confirm': True})
        self.assertEqual(clear_response.status_code, 200)
        self.assertEqual(self.client.get('/admin/data').get_json()['elections'], [])


if __name__ == '__main__':
    unittest.main()

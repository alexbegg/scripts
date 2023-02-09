import csv
import sys

import requests

if __name__ == '__main__':
    username = sys.argv[1]
    password = sys.argv[2]

    with requests.Session() as s:
        # Requests will 404 error unless we set a User-Agent
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36'
        })

        # Authenticate (Log in and set cookies for this session and get "sessionToken" for LoginSessionToken request)
        r1 = s.post(
            f"https://my.scouting.org/api/users/{username}/authenticate",
            headers={
                'Accept': 'application/json; version=2'
            },
            json={
                'password': password
            }
        )
        r1.raise_for_status()
        authenticate = r1.json()

        # LoginSessionToken (Get "SessionId" for "BSA-session" header for Membership request)
        session_token = authenticate['sessionToken']
        r2 = s.post(
            "https://my.scouting.org/_security/Security/LoginSessionToken",
            params={
                'RequestId': 'cc294fb0-59f8-46b7-242e-354693fb6cb8'
            },
            headers={
                'BSA-path': 'b25a6fed3fa2d6e18d86ee0fd2fd7026'
            },
            json={
                'SessionToken': session_token
            }
        )
        r2.raise_for_status()
        login_session_token = r2.json()

        # Membership (Get Organization Id for ChildOrganizations request)
        person_guid: str = authenticate['personGuid']
        session_id: str = login_session_token['items'][0]['SessionId']
        r3 = s.get(
            f"https://my.scouting.org/_security/SecurityV2/BackPack/Get/ID/{person_guid}",
            headers={
                'BSA-path': '6e433daf9e1afd6ffb1e0fc7b6489ce7',
                'BSA-session': session_id
            }
        )
        r3.raise_for_status()
        membership = r3.json()

        # ChildOrganizations (Get list of Units in the Organization)
        organization_id: str = membership['items'][0]['Id']
        r4 = s.get(
            f"https://api.scouting.org/organizations/{organization_id.upper()}/childOrganizations",
            params={
                'includeStem': 'true'
            },
            headers={
                'Authorization': f"Bearer {authenticate['token']}"
            }
        )
        r4.raise_for_status()
        child_organizations = r4.json()

        file_path = '/usr/src/app/unit_pins.csv'
        with open(file_path, 'w') as csvfile:
            rows = []
            for i, unit in enumerate(sorted(child_organizations, key=lambda d: d['organizationName'])):
                # Unit Pin (Get Unit Pin information for the Unit)
                organization_guid: str = unit['organizationGuid']
                r4 = s.get(
                    f"https://api.scouting.org/organizations/{organization_guid}/pin",
                    headers={
                        'Authorization': f"Bearer {authenticate['token']}"
                    }
                )
                r4.raise_for_status()
                unit_pin = r4.json()

                pin_information: dict = unit_pin['pinInformation']
                contact_person: dict = pin_information['contactPersons'][0] if pin_information['contactPersons'] else {}
                unit_information: dict = unit_pin['unitInformation']
                print(f"Saving pin information for {unit_information.get('name', '')}")
                row = {
                    'unit': unit_information.get('name', ''),
                    'current_chartered_org': unit_information['charterInformation']['communityOrganizationName'],
                    'pin_mode': 'Council' if pin_information['isPinModeCouncil'] else 'Unit',
                    'appear_on_beascout': 'TRUE' if pin_information['isPinActive'] else 'FALSE',
                    'apply_online': 'TRUE' if pin_information['isAcceptingOnlineRegistrations'] else 'FALSE',
                    'contact_person_name': f"{contact_person.get('firstName', '')} {contact_person.get('lastName', '')}",
                    'contact_person_phone': contact_person.get('phone', ''),
                    'contact_person_email': contact_person.get('email', ''),
                    'unit_website': pin_information.get('unitWebsite', ''),
                    'additional_unit_information': pin_information.get('unitAnnouncement', ''),
                    'meeting_address_line_1': pin_information.get('meetingAddressLine1', ''),
                    'meeting_address_line_2': pin_information.get('meetingAddressLine2', ''),
                    'meeting_city': pin_information.get('meetingCity', ''),
                    'meeting_state': pin_information.get('meetingState', ''),
                    'meeting_zip': pin_information.get('meetingZip', ''),
                    'latitude': pin_information.get('latitude', ''),
                    'longitude': pin_information.get('longitude', '')
                }
                rows.append(row)
            writer = csv.DictWriter(csvfile, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

import glob
import sys
import time
import urllib

import httplib2
from apiclient.discovery import build
from oauth2client.client import flow_from_clientsecrets


def get_name(contact_list_entry):
    name = "???"
    if contact_list_entry.get('names'):
        name = contact_list_entry['names'][0]['displayName']
        if contact_list_entry.get('emailAddresses'):
            name += " <{}>".format(contact_list_entry['emailAddresses'][0]['value'])
    elif contact_list_entry.get('emailAddresses'):
        name = contact_list_entry['emailAddresses'][0]['value']
    return name


def delete_contacts(people_service, one_at_a_time, pageToken=None, contacts_to_delete=None, time_elapsed=time.time()):
    contact_list = people_service.connections().list(resourceName='people/me', pageToken=pageToken, personFields='names,phoneNumbers,emailAddresses').execute()
    nextPageToken = contact_list.get('nextPageToken')
    totalItems = contact_list['totalItems']
    
    if one_at_a_time:
        for contact_list_entry in contact_list['connections']:
            name = get_name(contact_list_entry)

            if not contact_list_entry.get('phoneNumbers'):
                confirm = input("The contact \"{}\" has no phone number, do you want to delete it? (y/n): ".format(name))
                if confirm == 'y':
                    people_service.deleteContact(resourceName=contact_list_entry['resourceName']).execute()
                    print('deleted')
            else:
                phone_number = contact_list_entry.get('phoneNumbers')[0]['value']
                print("The contact \"{0}\" has a phone number of \"{1}\", skipping.".format(name, phone_number))

        if nextPageToken:
            delete_contacts(people_service, one_at_a_time, pageToken=nextPageToken)
    else:
        if not contacts_to_delete:
            contacts_to_delete = []
        time_elapsed = time.time() - time_elapsed
        # print(time_elapsed)
        print("Loading {} out of {}...".format(len(contacts_to_delete), totalItems))
        for contact_list_entry in contact_list['connections']:
            name = get_name(contact_list_entry)

            if not contact_list_entry.get('phoneNumbers'):
                contacts_to_delete.append((name, contact_list_entry['resourceName']))

        if nextPageToken:
            delete_contacts(people_service, one_at_a_time, pageToken=nextPageToken, contacts_to_delete=contacts_to_delete, time_elapsed=time_elapsed)
        else:
            if not one_at_a_time and contacts_to_delete:
                for name, resourceName in contacts_to_delete:
                    print("The contact \"{}\" has no phone number.".format(name))

                confirm = input("\nThe above {0} contacts have no phone number (out of {1} contacts), do you want to delete them? (y/n): ".format(len(contacts_to_delete), totalItems))
                if confirm == 'y':
                    print("")
                    for name, resourceName in contacts_to_delete:
                        people_service.deleteContact(resourceName=resourceName).execute()
                        print("deleted contact \"{0}\"".format(name, resourceName))
                        time.sleep(0.01)


if __name__ == '__main__':
    client_secret_file = sys.argv[1] if len(sys.argv) > 1 else None
    if not client_secret_file:
        client_secret_files = glob.glob('client_secret*.json')
        if client_secret_files:
            client_secret_file = client_secret_files[0]
    one_at_a_time = bool(len(sys.argv) > 2 and sys.argv[2] == 'yes')
    flow = flow_from_clientsecrets(client_secret_file,
                                   scope='https://www.googleapis.com/auth/contacts',
                                   redirect_uri='http://localhost:8080')
    auth_uri = flow.step1_get_authorize_url()
    print("To authenticate, visit this URL and authenticate the application with your Google account:\n\n{}\n".format(auth_uri))
    code = input("Then enter the returned \"code\" here (between '?code=' and '&scope='): ")
    print("")
    credentials = flow.step2_exchange(urllib.parse.unquote(code))

    http = httplib2.Http()
    http = credentials.authorize(http)

    service = build('people', 'v1', http=http)
    people_service = service.people()

    delete_contacts(people_service, one_at_a_time)

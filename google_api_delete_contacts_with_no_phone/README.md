# Google API - Delete All Contacts Without A Phone

This script will go through all your Google Contacts and list the contacts without a phone number and ask if you want to delete those contacts.

This script was created because there is no way in the Google Contacts website to filter contacts to only show ones without a phone number. So it is currently a manual process. This script allows you to delete those contacts in batches.

Because the script works in batches it might take multiple batches to delete those contacts. The script will show you how many it will delete in the current batch out of the total contacts found without a phone number.

## Create a Google API Console project and enable the People API

To get started using the Google People API, you need to first [use the setup tool](https://console.developers.google.com/start/api?id=people.googleapis.com&credential=client_key), which guides you through creating a project in the Google API Console, enabling the API, and creating credentials.

On the credentials page, provide these answers:

* Which API are you using? **People API**
* Where will you be calling the API from? **Web Browser (Javascript)**
* What data will you be accessing? **User data**

For more instructions visit https://developers.google.com/people/v1/getting-started

## Create authorization credentials (if not done already using the above setup tool)

1. Go to the [Credentials](https://console.developers.google.com/apis/credentials) page.
2. Click **Create credentials > OAuth client ID**.
3. Select the **Web application** application type.
4. For **Authorized JavaScript origins** enter `http://localhost:8080`.
5. For **Authorized redirect URIs** enter `http://localhost:8080`.
6. Click **Create**.
7. After creating your credentials, download the **client_secret.json** file from the API Console (using the download icon on the right).
8. Move the file into this current directory (all `client_secret*.json` files in this directory are Git-ignored)

For more instructions visit https://developers.google.com/identity/protocols/oauth2/web-server#creatingcred

## Running the script

Note: When authenticating with your Google account, it will redirect to a page that can't be reached. This is because the script is not running on a web server. Do not exit the browser, the authorization code is the part of the current URL between `?code=` and `&scope=`. The script will prompt you to enter that code.

### Option 1: Run it using Docker

1. Change to the current directory (if not in it already)

    ```shell
    cd google_api_delete_contacts_with_no_phone
    ```

2. Build the docker image

    ```shell
    docker build -t google_api_delete_contacts_with_no_phone .
    ```

3. Run the docker container

    ```shell
    docker run -it --rm google_api_delete_contacts_with_no_phone
    ```

### Option 2. Run it in a Python Virtual Environment

1. Set up and activate a python virtual environment

2. Change to the current directory (if not in it already)

    ```shell
    cd google_api_delete_contacts_with_no_phone
    ```

3. Install the requirements

    ```shell
    pip install -r requirements.txt
    ```

4. Run the script

    ```shell
    python google_api_delete_contacts_with_no_phone.py
    ```

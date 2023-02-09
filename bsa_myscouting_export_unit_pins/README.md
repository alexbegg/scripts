# BSA my.Scouting Export Unit Pins

A Python script that exports the ["Be A Scout"](https://beascout.scouting.org/) Unit Pin information for all units you have visibility in [my.Scouting](https://my.scouting.org/) and saves them to a CSV file named `unit_pins.csv`.

## Run this with docker

1. Change to the current directory (if not in it already)

    ```shell
    cd bsa_myscouting_export_unit_pins
    ```

2. Build the docker image

    ```shell
    docker build -t bsa_myscouting_export_unit_pins .
    ```

3. Run the docker container

    (Replace `USER_NAME` and `PASSWORD` with your [my.Scouting](https://my.scouting.org/) Username and Password)

    ```shell
    docker run -it --rm --name bsa_myscouting_export_unit_pins -v $(pwd):/usr/src/app bsa_myscouting_export_unit_pins "USER_NAME" "PASSWORD"
    ```

It will make API calls to [my.Scouting](https://my.scouting.org/) and then save the ["Be A Scout"](https://beascout.scouting.org/) Unit Pin information to a CSV file named `unit_pins.csv`.

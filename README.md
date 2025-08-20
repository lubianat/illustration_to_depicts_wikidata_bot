# Illustrations to QuickStatements

## Description

This project processes Wikimedia Commons botanical illustrations categories and updates Wikidata based on the number of images found in each category. It uses the WikibaseIntegrator library to interact with Wikidata and updates items with relevant properties.

## Installation

1. Clone the repository:
    ```bash
    git clone https://github.com/lubianat/illustrations_to_quickstatements.git
    cd illustrations_to_quickstatements
    ```

2. Install the required dependencies:
    ```bash
    pip install -r requirements.txt
    ```

3. Create a `login.py` file with your Wikidata credentials:
    ```python
    USERNAME = 'your_username'
    PASSWORD = 'your_password'
    ```

## Usage

Run the script with the desired Wikimedia Commons category:
```bash
python illustrations_to_quickstatements.py <category> [--verbose]

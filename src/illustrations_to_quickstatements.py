import re
import logging
import click
import yaml
from tqdm import tqdm
from pathlib import Path
import urllib.parse 

import random 
from login import USERNAME, PASSWORD
from helper import (
    get_subcategories,
    fetch_wikidata_item,
    get_files_in_category,
    get_existing_claims,
    build_commons_file_permalink
)
from wikibaseintegrator import wbi_login, WikibaseIntegrator, wbi_enums
from wikibaseintegrator.datatypes import CommonsMedia, Item, URL
from wikibaseintegrator.wbi_config import config as wbi_config
from wikibaseintegrator.models import References, Reference

# Configure WikibaseIntegrator for Wikidata
wbi_config['MEDIAWIKI_API_URL'] = 'https://www.wikidata.org/w/api.php'
wbi_config['SPARQL_ENDPOINT_URL'] = 'https://query.wikidata.org/sparql'
wbi_config['WIKIBASE_URL'] = 'https://www.wikidata.org'
wbi_config['USER_AGENT'] = 'TiagoLubianaBot (https://meta.wikimedia.org/wiki/User:TiagoLubianaBot)'
BATCH_OF_FIFTY_FLAG = 0 

# Login as TiagoLubianaBot
login_instance = wbi_login.Login(
    user=USERNAME,
    password=PASSWORD,
    mediawiki_api_url=wbi_config['MEDIAWIKI_API_URL']
)
wbi = WikibaseIntegrator(login=login_instance)

# Path to YAML file for categories with 3+ images
YAML_PATH = Path("categories_to_review.yaml")

def generate_editgroup_snippet():
    # As per https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool
    random_hex = f"{random.randrange(0, 2**48):x}"
    editgroup_snippet = f"([[:toolforge:editgroups/b/CB/{random_hex}|details]])"
    return editgroup_snippet

@click.command()
@click.argument('category')
@click.option('--verbose', is_flag=True, help='Enable verbose output for debugging.')
def process_family_category(category, verbose):
    """
    CLI tool to process Wikimedia Commons categories and update Wikidata
    based on the number of images found in each category.
    """
    global BATCH_OF_FIFTY_FLAG
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)
    logging.info(f"Processing category: {category}")

    categories_to_review = {}

    # 1) Fetch subcategories recursively
    genera = get_subcategories(category, verbose=verbose)
    logging.info(f"Top-level category has {len(genera)} subcategories.")
    # 2) Process each subcategory (genus) → get sub-subcategories (taxa)
    for genus in tqdm(genera, desc="Processing genera"):
        if BATCH_OF_FIFTY_FLAG == 0:
            edit_group_snippet = generate_editgroup_snippet()
        if "Unidentified" in genus:
            continue

        taxa = get_subcategories(genus, verbose=verbose)

        for taxon in tqdm(taxa, desc=f"Processing taxa in {genus}", leave=False):
            match = re.match(r"([^\\-]+) - botanical illustrations", taxon)
            if not match:
                match = re.match(r"([^\\-]+) botanical illustrations", taxon)
                if not match:
                    match = re.match(r"([^\\-]+) \(illustrations\)", taxon)
                    if not match: 
                        logging.info(f"Skipping taxon {taxon}: no match for regex.")
                        continue

            species_name = match.group(1)
            wikidata_item = fetch_wikidata_item(species_name, verbose=verbose)

            if not wikidata_item:
                continue

            # 3) Get all files in the taxon category
            files = get_files_in_category(taxon, verbose=verbose)
            file_count = len(files)

            if file_count == 1 or file_count == 2:
                add_depicts_or_illustration_statements(wikidata_item, files, edit_group_snippet)
            elif file_count >= 3:
                categories_to_review[taxon] = files
                save_to_yaml(categories_to_review)

    print("Processing complete. Wikidata updated.")

def add_depicts_or_illustration_statements(wikidata_item: str, files: list, edit_group_snippet=""):
    """
    Determine which properties (P18 or P13162) should be added to a
    Wikidata item and do so in one write, if needed.
    """
    global BATCH_OF_FIFTY_FLAG
    # 1) Check existing claims for P18/P13162
    existing_p18 = get_existing_claims(wikidata_item, "P18")
    existing_p13162 = get_existing_claims(wikidata_item, "P13162")

    # 2) Decide what to add (none, P18, or P13162)
    add_prop = None
    if not existing_p18 and not existing_p13162:
        add_prop = "P18"
    elif existing_p18 and not existing_p13162:
        add_prop = "P13162"
    # test if the file is already in the P18 or P13162:
    for file in files:
        parsed_file = urllib.parse.quote(file)
        print(parsed_file)
        print(existing_p13162)
        print(existing_p18)
        if parsed_file in existing_p18 or parsed_file in existing_p13162:
            files.remove(file)

    if not add_prop:
        # Means we have both P18 and P13162 => skip
        logging.info(f"Skipping {wikidata_item}, already has P18 and P13162.")
        return

    # 3) Fetch item data once, add claims, then write
    item = wbi.item.get(entity_id=wikidata_item)

    for file_name in files:
        references = create_reference(file_name)

        item.claims.add(
            CommonsMedia(prop_nr=add_prop, value=file_name, references=references),
            action_if_exists=wbi_enums.ActionIfExists.MERGE_REFS_OR_APPEND
        )

    summary = f"Adding {add_prop} claims inferred from Commons. {edit_group_snippet}"
    if files:
        try:
            logging.info(f"Updating {wikidata_item}: Adding {add_prop} for {files}")
            item.write(summary=summary)
            BATCH_OF_FIFTY_FLAG +=1
            if BATCH_OF_FIFTY_FLAG == 5:
                BATCH_OF_FIFTY_FLAG = 0
        except Exception as e:
            logging.error(f"Failed to update {wikidata_item}: {e}")

def create_reference(file_name):
    """
    Creates a reference object, 'Inferred from Wikimedia Commons' (P887=Q131478853).
    """
    references = References()
    ref_obj = Reference()
    ref_obj.add(Item(prop_nr="P887", value="Q131478853"))  # Inferred from Wikimedia Commons
    commons_permalink = build_commons_file_permalink(file_name)
    ref_obj.add(URL(prop_nr="P4656", value=commons_permalink))
    references.add(ref_obj)
    return references

def save_to_yaml(data: dict):
    """
    Saves categories with 3+ images to a YAML file so they can be handled manually.
    """
    if YAML_PATH.exists():
        with YAML_PATH.open("r", encoding="utf-8") as f:
            existing_data = yaml.safe_load(f) or {}
    else:
        existing_data = {}

    existing_data.update(data)

    with YAML_PATH.open("w", encoding="utf-8") as f:
        yaml.dump(existing_data, f, default_flow_style=False, allow_unicode=True)

    logging.info(f"Saved categories with 3+ images to {YAML_PATH}")

if __name__ == '__main__':
    process_family_category()

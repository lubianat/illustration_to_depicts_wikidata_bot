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
    get_qid_from_taxon_name,
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

# Login as TiagoLubianaBot
login_instance = wbi_login.Login(
    user=USERNAME,
    password=PASSWORD,
    mediawiki_api_url=wbi_config['MEDIAWIKI_API_URL']
)
wbi = WikibaseIntegrator(login=login_instance)

# Path to YAML file for categories with 3+ images
YAML_PATH = Path("categories_to_review.yaml")
# Files to store processed entities
PROCESSED_SPECIES_PATH = Path("processed_species.yaml")
PROCESSED_FAMILIES_PATH = Path("processed_families.yaml")
PROCESSED_GENERA_PATH = Path("processed_genera.yaml")

def load_processed_entities(entity_type):
    """
    Load the set of processed entities (species, genera, or families) names from the YAML file.
    Returns a set of entity names (strings).
    """
    if entity_type == "species":
        path = PROCESSED_SPECIES_PATH
    elif entity_type == "families":
        path = PROCESSED_FAMILIES_PATH
    elif entity_type == "genera":
        path = PROCESSED_GENERA_PATH
    else:
        raise ValueError(f"Unknown entity type: {entity_type}")

    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return set(data) if data else set()
    return set()

def save_processed_entities(processed_entities: set, entity_type):
    """
    Save the set of processed entities (species, genera, or families) names to the YAML file.
    """
    if entity_type == "species":
        path = PROCESSED_SPECIES_PATH
    elif entity_type == "families":
        path = PROCESSED_FAMILIES_PATH
    elif entity_type == "genera":
        path = PROCESSED_GENERA_PATH
    else:
        raise ValueError(f"Unknown entity type: {entity_type}")

    with path.open("w", encoding="utf-8") as f:
        yaml.dump(list(processed_entities), f, default_flow_style=False, allow_unicode=True)

def generate_editgroup_snippet():
    # As per https://www.wikidata.org/wiki/Wikidata:Edit_groups/Adding_a_tool
    random_hex = f"{random.randrange(0, 2**48):x}"
    editgroup_snippet = f"([[:toolforge:editgroups/b/CB/{random_hex}|details]])"
    return editgroup_snippet

def process_family_category(category, verbose, edit_group_snippet):
    """
    CLI tool to process Wikimedia Commons categories and update Wikidata
    based on the number of images found in each category.
    """
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)
    logging.info(f"Processing category: {category}")

    categories_to_review = {}
    # Load the already processed species names
    processed_species = load_processed_entities("species")
    # Load the already processed genera
    processed_genera = load_processed_entities("genera")

    # 1) Fetch subcategories recursively (these are the genera)
    genera = get_subcategories(category, verbose=verbose)
    logging.info(f"Top-level category has {len(genera)} subcategories.")

    # 2) Process each genus and its taxa (species)
    for genus in tqdm(genera, desc="Processing genera"):
        if "Unidentified" in genus or genus in processed_genera:
            logging.info(f"Skipping already processed genus: {genus}")
            continue

        taxa = get_subcategories(genus, verbose=verbose)
        for taxon in tqdm(taxa, desc=f"Processing taxa in {genus}", leave=False):
            # Use regex to extract the species name from the category title
            match = re.match(r"([^\\-]+) - botanical illustrations", taxon)
            if not match:
                match = re.match(r"([^\\-]+) botanical illustrations", taxon)
                if not match:
                    match = re.match(r"([^\\-]+) \(illustrations\)", taxon)
                    if not match: 
                        logging.info(f"Skipping taxon {taxon}: no match for regex.")
                        continue

            species_name = match.group(1).strip()
            # Skip processing if this species has already been handled
            if species_name in processed_species:
                logging.info(f"Skipping already processed species: {species_name}")
                continue

            wikidata_item = get_qid_from_taxon_name(species_name, verbose=verbose)
            if not wikidata_item:
                # Even if no Wikidata item is found, mark species as processed to avoid rechecking it later
                processed_species.add(species_name)
                save_processed_entities(processed_species, "species")
                continue

            # 3) Get all files in the taxon category
            files = get_files_in_category(taxon, verbose=verbose)
            file_count = len(files)

            if file_count == 1 or file_count == 2:
                add_depicts_or_illustration_statements(wikidata_item, files, edit_group_snippet)
            elif file_count >= 3:
                categories_to_review[taxon] = files
                save_to_yaml(categories_to_review)

            # Mark species as processed and update the file
            processed_species.add(species_name)
            save_processed_entities(processed_species, "species")

        # Mark genus as processed after all its taxa are handled
        processed_genera.add(genus)
        save_processed_entities(processed_genera, "genera")

    print("Processing complete. Wikidata updated.")

def add_depicts_or_illustration_statements(wikidata_item: str, files: list, edit_group_snippet=""):
    """
    Determine which property (P18 or P13162) should be added to a
    Wikidata item and add it in one write, if needed.
    """
    # 1) Check existing claims for P18/P13162
    existing_p18 = get_existing_claims(wikidata_item, "P18")
    existing_p13162 = get_existing_claims(wikidata_item, "P13162")

    # 2) Decide which property to add
    add_prop = None
    if not existing_p18 and not existing_p13162:
        add_prop = "P18"
    elif existing_p18 and not existing_p13162:
        add_prop = "P13162"

    # Remove any file that is already claimed
    for file in files.copy():
        parsed_file = urllib.parse.quote(file)
        if parsed_file in existing_p18 or parsed_file in existing_p13162:
            files.remove(file)

    if not add_prop:
        logging.info(f"Skipping {wikidata_item}, already has P18 and P13162.")
        return

    # 3) Fetch the Wikidata item, add claims, then write
    item = wbi.item.get(entity_id=wikidata_item)
    for file_name in files:
        references = create_reference(file_name)
        item.claims.add(
            CommonsMedia(prop_nr=add_prop, value=file_name, references=references),
            action_if_exists=wbi_enums.ActionIfExists.MERGE_REFS_OR_APPEND
        )

    summary = f"Adding {add_prop} claims inferred from Commons. {edit_group_snippet}"
    if len(files) > 0:
        try:
            logging.info(f"Updating {wikidata_item}: Adding {add_prop} for {files}")
            item.write(summary=summary)
        except Exception as e:
            logging.error(f"Failed to update {wikidata_item}: {e}")

def create_reference(file_name):
    """
    Creates a reference object: 'Inferred from Wikimedia Commons' (P887=Q131478853)
    along with the file permalink.
    """
    references = References()
    ref_obj = Reference()
    ref_obj.add(Item(prop_nr="P887", value="Q131478853"))
    commons_permalink = build_commons_file_permalink(file_name)
    ref_obj.add(URL(prop_nr="P4656", value=commons_permalink))
    references.add(ref_obj)
    return references

def save_to_yaml(data: dict):
    """
    Saves categories with 3+ images to a YAML file for manual review.
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
    edit_group_snippet = generate_editgroup_snippet()
    list_of_categories = get_subcategories("Botanical illustrations by family", verbose=True)

    families = load_processed_entities("families")
    for category in list_of_categories:
        print(f"####### Processing category: {category} #######")
        if category in families:
            print(f"Skipping already processed family: {category}")
            continue
        process_family_category(category, True, edit_group_snippet)
        families.add(category)
        save_processed_entities(families, "families")

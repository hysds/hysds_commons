import copy
import json
import os
from shapely.geometry import shape
from hysds_commons.log_utils import logger

def generate_bbox(coord_list):

# def generate_bbox(coord_list)
# This function creates a bounding box for an input list of coordinates describing a polygon.  The function is called within the create_stac_doc function.
    '''
    :param coord_list (dict): a list of coordinates where each coordinate is expressed as x, y
    :return ret (list): the bbox created for the input polygon
    '''
    box = []
    for i in (0, 1):
         res = sorted(coord_list, key=lambda x: x[i])
         box.append((res[0][i], res[-1][i]))
    ret = f"({box[0][0]}, {box[1][0]}, {box[0][1]}, {box[1][1]})"
    return ret


def create_stac_doc(product_directory, metadata, mapping, assets_desc, product_type, product_path, lineage):
    stac_doc = dict()
    '''
    :param product_directroy (str): Name of product downloaded into work directory
    :param metadata (dict): .met.json file content of the product
    :param mapping (dict): file content of stac_mappings.json
    :param assets_desc (dict): file content of assets_description.json
    :param product_type (str): product / dataset type
    :param product_path (str): S3 location or URL of product
    :param lineage (list): list of URLs pointing to location of every file used as input to generate the product
    :return stac_doc (dict): it is the STAC JSON for the input product, compliant with STAC requirements for an item
    '''

    # Creating stac doc based on mapping configuration of project
    # 'field_mappings' Defines a 1:1 mapping between the expected field name in STAC to the 
    # metadata key found in the product's met.json file.
    field_mapping = mapping.get("field_mappings")
    for k in field_mapping:
        stac_doc[k] = metadata.get(field_mapping.get(k))

    stac_links = []
    # 'code_mappings' derives values for certain STAC fields when they are not straight forward mappings. 
    # The value provided is a python code snippet which is evaluated in the create_stac_doc 
    # function.
    code_mapping = mapping.get("code_mappings")
    for k in code_mapping:
        stac_doc[k] = eval(str(code_mapping.get(k)))
        # set up links
        if k == "links":
            for l in code_mapping.get("links"):
                link = dict()
                link["ref"] = l
                if l == "root":
                    link["href"] = code_mapping.get("links").get("root")
                else:
                    link["href"] = eval(code_mapping.get("links").get(l))
                stac_links.append(link)
            stac_doc["links"] = stac_links

    # Creating properties. Copy over all the metadata EXCLUDING Properties and Geometry
    properties = copy.deepcopy(metadata)
    del properties[field_mapping.get("id")]
    # This try-except block is included because not every product will have bounding polygon
    try:
        del properties[field_mapping.get("geometry")]
    except KeyError:
        logger.info("Bounding Polygon not found in metadata. Setting to global extent")
    stac_doc["properties"] = properties

    # Set geometry, if doesn't exist in product then set to global coordinates
    if metadata.get(field_mapping.get("geometry")) is not None:
        stac_doc["geometry"] = metadata.get(field_mapping.get("geometry"))
    else:
        stac_doc["geometry"] = {
            "type": "Polygon",
            "coordinates": [[[-180, -90], [-180, 90], [180, 90], [180, -90], [-180, -90]]]
        }

    # assign to stac to generate_bbox
    stac_doc["bbox"] = list(shape(stac_doc.get("geometry")).bounds)

    # Start generating assets
    assets = dict()

    # get assets description based on product type
    prod_desc = assets_desc.get(product_type)

    # Get list of filenames in product dir
    files = os.listdir(product_directory)

    # iterate over file info for a product type
    for file_ext in prod_desc:
        file_info = prod_desc.get(file_ext)
        for file in files:
            if file.endswith(file_ext):
                assets_key = file_info.get("name")
                assets_value = {
                    "href": f"{product_path}/{file}",
                    "title": file_info.get("title"),
                    "type": file_info.get("type")
                }
                assets[assets_key] = assets_value
    stac_doc["assets"] = assets

    logger.info("created stac document: %s" % json.dumps(stac_doc, indent=4))

    return stac_doc

import copy
import os
import json
from hysds_commons.log_utils import logger

def bbox(coord_list):
    box = []
    for i in (0, 1):
        res = sorted(coord_list, key=lambda x: x[i])
        box.append((res[0][i], res[-1][i]))
    ret = f"({box[0][0]}, {box[1][0]}, {box[0][1]}, {box[1][1]})"
    return ret

def create_stac_doc(product_directory, metadata, mapping, assets_desc):
    stac_doc = dict()

    # Creating stac doc based on mapping configuration of project
    field_mapping = mapping.get("field_mappings")
    for k in field_mapping:
        stac_doc[k] = metadata.get(field_mapping.get(k))

    stac_links = []
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

    # Creating properties. Copy over all the metadata except Product ID and Geo
    properties = copy.deepcopy(metadata)
    del properties[field_mapping.get("id")]
    # Not every product will have bounding polygon
    try:
        del properties[field_mapping.get("geometry")]
    except KeyError:
        logger.info("Bounding Polygon not found in metadata. Setting to global extent")
    stac_doc["properties"] = properties

    # Set geometry, if doesn't exist in product then set to world
    if metadata.get(field_mapping.get("geometry")) is not None:
        stac_doc["geometry"] = metadata.get(field_mapping.get("geometry"))
    else:
        stac_doc["geometry"] = {
            "type": "Polygon",
            "coordinates": [[[-180, -90], [-180, 90], [180, 90], [180, -90], [-180, -90]]]
        }

    # assign to stac.bbox
    stac_doc["bbox"] = bbox(stac_doc.get("geometry").get("coordinates")[0])

    # Start generating assets.
    assets = dict()

    # get assets description based on product type
    product_type = eval(mapping.get("other_mappings").get("product_type"))
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
                    "href": file,
                    "title": file_info.get("title"),
                    "type": file_info.get("type")
                }
                assets[assets_key] = assets_value
    stac_doc["assets"] = assets

    logger.info("created stac document: %s" % json.dumps(stac_doc, indent=4))

    return stac_doc

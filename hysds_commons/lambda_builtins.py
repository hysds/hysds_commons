'''
This module defines functions that can be used by the lambda processors
defined in the HySDS-IO files. It is an attempt to absract out common
functions that are used in most HySDS processing in order to be shared
across lambda implementations.

@author mstarch
'''
from future import standard_library
standard_library.install_aliases()


def get_best_url(urls, best_prefix=None):
    '''
    Return a single URL starting with the given prefix. If the prefix is None
    then return a URL not starting with "http". If all URLs start with "http"
    return the first.
    @param urls: list of urls to search
    #param best_prefix: prefix to look for
    @returns: best url
    '''
    best = None
    for url in urls:
        # Return matching best
        if not best_prefix is None and url.startswith(best_prefix):
            return url
        # Set best as first not http
        if best is None or (not url.startswith("http") and best.startswith("http")):
            best = url
    return best


def get_partial_products(ident, base_url, product_relative_paths):
    '''
    Get a list of URLs to paths under the base URL
    @param base_url: base url of the product
    @param product_relative_paths: list of product relative paths
    @returns: list of full paths
    '''
    import os.path
    localize = []
    for rel_path in product_relative_paths:
        localize.append({
            "url": os.path.join(base_url, rel_path),
            "local_path": os.path.join(ident, rel_path)
        })
    return localize


def region_to_bbox(region):
    '''
    Converts from a hysdsio region type to a bbox.
    @param region: region in GeoJSON format containing
                   a "coordinates" child
    @return: MBR in [minLat, maxLat, minLon, maxLon] format
    '''
    coordinates = region.get("coordinates")
    min_lat = 90
    max_lat = -90
    min_lon = 360
    max_lon = -360
    # Must handle multi-shape coordinats
    for shape in coordinates:
        for point in shape:
            min_lat = min(min_lat, point[1])
            max_lat = max(max_lat, point[1])
            min_lon = min(min_lon, point[0])
            max_lon = max(max_lon, point[0])
    return [min_lat, max_lat, min_lon, max_lon]

import hysds_commons.metadata_rest_utils


CONTAINER_INDEX = "containers"
CONTAINER_TYPE = "container"


def get_container_types(es_url, logger=None):
    '''
    Get the container listings from Elastic Search
    @param es_url - elasticsearch URL
    @return: list of container ids
    '''
    return hysds_commons.metadata_rest_utils.get_types(es_url, CONTAINER_INDEX,
                                                       logger=logger)


def get_container(es_url, ident, logger=None):
    '''
    Get a container (JSON body)
    @param es_url - elasticsearch URL
    @param ident - identity of container
    @return: dict representing anonymous object of HySDS IO
    '''
    return hysds_commons.metadata_rest_utils.get_by_id(es_url, CONTAINER_INDEX,
                                                       CONTAINER_TYPE, ident,
                                                       logger=logger)


def add_container(es_url, name, url, version, digest, logger=None):
    '''
    Ingests a container into the Mozart ElasticSearch index
    @param es_url - elasticsearch URL
    @param name - name of object for ingestion into ES
    @param url - url of object for ingestion into ES
    @param version - version of object for ingestion into ES
    @param digest - sha256 digest ID of container image
    '''
    return hysds_commons.metadata_rest_utils.add_metadata(es_url, CONTAINER_INDEX,
                                                          CONTAINER_TYPE, {
                                                              "id": name,
                                                              "digest": digest,
                                                              "url": url,
                                                              "version": version},
                                                          logger=logger)


def remove_container(es_url, ident, logger=None):
    '''
    Remove a container
    @param es_url - elasticsearch URL
    @param ident - id to delete
    '''
    return hysds_commons.metadata_rest_utils.remove_metadata(es_url, CONTAINER_INDEX,
                                                             CONTAINER_TYPE, ident,
                                                             logger=logger)

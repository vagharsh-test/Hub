import os
from glob import glob

import hub
from hub.auto.util import get_parsers

__all__ = ['infer_dataset']

_directory_parsers = get_parsers()


def _find_root(path):
    """
    find the root of the dataset within the given path.
    the "root" is defined as being the path to a subdirectory within path that has > 1 folder/file (if applicable).

    in other words, if there is a directory structure like:
    dataset -
        Images -
            class1 -
                img.jpg
                ...
            class2 -
                img.jpg
                ...
            ...

    the output of this function should be "dataset/Images/" as that is the root.
    """

    subs = glob(os.path.join(path, '*'))
    hub_dir = os.path.join(path, 'hub')
    if hub_dir in subs:
        subs.remove(hub_dir)  # ignore the hub directory
    if len(subs) > 1:
        return path
    return _find_root(subs[0])


def infer_dataset(path):
    # TODO: handle s3 path

    if not os.path.isdir(path):
        raise Exception('input path must be either a directory')

    hub_path = os.path.join('./', path, 'hub')

    if os.path.isdir(hub_path):
        print('inferred dataset found in "%s", using that' % hub_path)
        return hub.Dataset(hub_path)

    root = _find_root(path)
    ds = None

    # go through all functions created using the `directory_parser` decorator in
    # `hub.schema.auto.directory_parsers`
    for parser in _directory_parsers:
        ds = parser(root)
        if ds is not None:
            break

    if ds is None:
        raise Exception(
            'could not infer dataset for the root "%s". either add a new parser to'
            % root +
            '`hub.schema.auto.directory_parsers` or write a custom transform + schema.'
        )

    return ds.store(hub_path)  # TODO: handle s3
from fsspec import get_mapper
from zarr.meta import encode_array_metadata, encode_group_metadata
from zarr.storage import array_meta_key, group_meta_key, attrs_key
from zarr.util import json_dumps
import numpy as np
import imageio

import os
from pathlib import Path
from urllib.parse import urlparse, urlunparse
import xml.etree.ElementTree as ET
from collections import namedtuple

DZIMetadata = namedtuple("DZIMetadata", "tilesize overlap format height width")


def create_root_attrs(levels):
    datasets = [{"path": str(i)} for i in levels]
    return {"multiscales": [{"datasets": datasets, "version": "0.1"}]}


def parseDZI(xml_str: str):
    Image = ET.fromstring(xml_str)
    Size = Image[0]
    return DZIMetadata(
        tilesize=int(Image.get("TileSize")),
        overlap=int(Image.get("Overlap")),
        format=Image.get("Format"),
        width=int(Size.get("Width")),
        height=int(Size.get("Height")),
    )


class DZIStore:
    def __init__(self, path: str, storage_options=None):
        storage_options = storage_options or {}

        if os.path.isfile(path):
            # Local DZI
            local_path = Path(path)
            self._dzi_fmap = get_mapper(str(local_path.parent), **storage_options)
            self._files_prefix = local_path.stem
        else:
            # Remote DZI (http/https, gc, s3, etc...)
            url = urlparse(path)
            url_path = Path(url.path)
            new_url = urlunparse(
                (
                    url.scheme,
                    url.netloc,
                    str(url_path.parent),
                    url.params,
                    url.query,
                    url.fragment,
                )
            )
            self._dzi_fmap = get_mapper(new_url, **storage_options)
            self._files_prefix = url_path.stem

        self._dzi_meta = parseDZI(self._dzi_fmap[f"{self._files_prefix}.dzi"])
        self._metadata_store = self._init_zarr_metadata()

    def __getitem__(self, key):
        if key in self._metadata_store:
            return self._metadata_store[key]

        try:
            level, chunk_key = key.split("/")
            ykey, xkey, _ = map(int, chunk_key.split("."))

            dzi_path = self._get_dzi_path(level, xkey, ykey)
            tile = self._get_image_tile(dzi_path)
            trimmed_tile = self._normalize_chunk(tile, xkey, ykey)
            return trimmed_tile.tobytes()
        except:
            raise KeyError

    def __setitem__(self, key, value):
        raise NotImplementedError

    def _get_dzi_path(self, level: int, x: int, y: int) -> str:
        img_format = self._dzi_meta.format
        return f"{self._files_prefix}_files/{level}/{x}_{y}.{img_format}"

    def _get_image_tile(self, img_path: str) -> np.ndarray:
        # Get file from store
        cbytes = self._dzi_fmap[img_path]
        # Decode image
        return imageio.imread(cbytes)

    def _normalize_chunk(self, arr: np.ndarray, x: int, y: int) -> np.ndarray:
        # DZI images have overlapping tiles.
        # https://github.com/openseadragon/openseadragon/wiki/The-DZI-File-Format#overlap
        # Here we trim overlapping tiles or pad edge tiles based on the chunk key.
        ysize, xsize, _ = arr.shape
        tilesize, overlap = self._dzi_meta.tilesize, self._dzi_meta.overlap

        if xsize == tilesize and ysize == tilesize:
            # Decoded image is already correct size.
            return arr

        # TODO: There is probably a more elegant way to do this...
        view = arr
        if xsize - tilesize == 2 * overlap:
            # Inner x; overlap on left and right
            view = view[:, overlap:-overlap, :]

        if ysize - tilesize == 2 * overlap:
            # Inner y; overlap on top and bottom
            view = view[overlap:-overlap, :, :]

        if xsize - tilesize == overlap:
            # Edge x; overlap on left or right
            xslice = slice(None, -overlap) if x == 0 else slice(overlap, None)
            view = view[:, xslice, :]

        if ysize - tilesize == overlap:
            # Edge y; overlap on top or bottom
            yslice = slice(None, -overlap) if y == 0 else slice(overlap, None)
            view = view[yslice, :, :]

        if view.shape[0] < tilesize or view.shape[1] < tilesize:
            # Tile is smaller than tilesize; Needs to be padded.
            y_pad = tilesize - view.shape[0]
            x_pad = tilesize - view.shape[1]
            return np.pad(view, ((0, y_pad), (0, x_pad), (0, 0)))

        return view

    def _get_csize(self, max_level):
        # Can't determine whether PNG will be RGB/RGBA just from file suffix
        # Here we use the first image in the pyramid as a representative tile.
        dzi_path = self._get_dzi_path(max_level, 0, 0)
        tile = self._get_image_tile(dzi_path)
        return tile.shape[2]

    def _init_zarr_metadata(self) -> dict:
        store = dict()
        # DZI generates all levels of the pyramid
        # Level 0 is 1x1 image, so we need to calculate the max level (highest resolution)
        # and trim the pyramid to just the tiled levels.
        max_level = np.ceil(
            np.log2(max(self._dzi_meta.width, self._dzi_meta.height))
        ).astype(int)
        nlevels = max_level - np.ceil(np.log2(self._dzi_meta.tilesize)).astype(int)
        levels = list(reversed(range(max_level + 1)))[:nlevels]

        store[group_meta_key] = encode_group_metadata()
        store[attrs_key] = json_dumps(create_root_attrs(levels))

        # TODO: Might be a better way to determine RGB/RGBA-ness for pyramid
        csize = self._get_csize(max_level)
        for level in range(nlevels):
            xsize, ysize = (
                self._dzi_meta.width // 2 ** level,
                self._dzi_meta.height // 2 ** level,
            )
            meta = dict(
                chunks=(self._dzi_meta.tilesize, self._dzi_meta.tilesize, csize),
                shape=(ysize, xsize, csize),
                compressor=None,  # chunk is decoded with store, so no zarr compression
                dtype=np.dtype(np.uint8),  # RGB/A images only
                fill_value=0,
                filters=None,
                order="C",
            )
            key_prefix = str(max_level - level) + "/"
            store[key_prefix + array_meta_key] = encode_array_metadata(meta)

        return store

    def keys(self):
        return self._metadata_store.keys()

    def __iter__(self):
        return iter(self._metadata_store)

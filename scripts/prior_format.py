"""Stable five-column prior validation, independent of PlantVelo installation."""
import csv
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path
import re

FIELDS=('gene_id','chromosome','start','end','strand')

@dataclass(frozen=True)
class PriorKey:
    gene_id:str
    chromosome:str
    start:int
    end:int
    strand:str

@dataclass(frozen=True)
class PriorRecord:
    key:PriorKey

@dataclass(frozen=True)
class PriorTable:
    records:tuple
    sha256:str
    fieldnames:tuple=FIELDS


def load_builtin_ir_prior(path,species=None):
    """Validate the published schema and normalize coordinates for matching."""
    data=Path(path).read_bytes();reader=csv.DictReader(io.StringIO(data.decode('utf-8')),delimiter='\t')
    if reader.fieldnames!=list(FIELDS):raise ValueError('prior requires exactly five ordered columns')
    records=[];seen=set();strands={}
    for row in reader:
        if None in row or any(v is None for v in row.values()):raise ValueError('malformed prior row')
        row={k:v.strip()for k,v in row.items()}
        if (not row['gene_id'] or not row['chromosome'] or row['strand']not in {'+','-'}
                or not re.fullmatch('[0-9]+',row['start'])or not re.fullmatch('[0-9]+',row['end'])
                or not 1<=int(row['start'])<=int(row['end'])):raise ValueError('invalid prior interval')
        key=PriorKey(row['gene_id'],row['chromosome'],int(row['start'])-1,int(row['end']),row['strand'])
        if key in seen:raise ValueError('duplicate prior interval')
        coordinate=(key.gene_id,key.chromosome,key.start,key.end)
        if coordinate in strands and strands[coordinate]!=key.strand:raise ValueError('same-gene prior strand conflict')
        strands[coordinate]=key.strand;seen.add(key);records.append(PriorRecord(key))
    if not records:raise ValueError('empty high-confidence prior')
    return PriorTable(tuple(records),hashlib.sha256(data).hexdigest())

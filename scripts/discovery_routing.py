"""Choose the original archive for discovery; file-download mirrors are separate."""
import re

PREFIXES={'SRR':'ncbi','ERR':'ena','DRR':'ddbj','CRR':'ngdc'}
IMPLEMENTED={'ncbi','ena','ddbj','ngdc'}


def origin(accession):
    if not re.fullmatch(r'(?:SRR|ERR|DRR|CRR)[0-9]+',accession):
        raise ValueError('invalid run accession: '+accession)
    return PREFIXES[accession[:3]]


def project_origin(project):
    for pattern,source in [(r'(?:PRJNA|SRP)[0-9]+','ncbi'),(r'(?:PRJEB|ERP)[0-9]+','ena'),
                           (r'(?:PRJD[AB]|DRP)[0-9]+','ddbj'),(r'PRJCA[0-9]+','ngdc')]:
        if re.fullmatch(pattern,project):return source
    raise ValueError('invalid project accession: '+project)


def resolve_sources(source,accessions,projects):
    if source not in {'auto','ncbi','ena','ddbj','ngdc'}:raise ValueError('unknown discovery source')
    runs={a:origin(a)for a in sorted(set(accessions))}
    project_sources={p:project_origin(p)for p in sorted(set(projects))}
    if source!='auto':
        for accession,owner in runs.items():
            if owner!=source:raise ValueError('{} belongs to {}; cannot retrieve its metadata from {}'.format(accession,owner,source))
        selected={source}
    else:
        selected=set(runs.values())or set(project_sources.values())or IMPLEMENTED
        if runs and projects and not set(runs.values())<=set(project_sources.values()):
            raise ValueError('project filters do not match every selected run archive; use an explicit source for cross-archive projects')
    for owner in sorted(selected):
        if owner not in IMPLEMENTED:raise RuntimeError(owner.upper()+' native discovery is not implemented; no cross-database fallback')
    return {owner:{'accessions':[a for a,s in runs.items()if s==owner],
                   'projects':list(project_sources)if source!='auto'else[p for p,s in project_sources.items()if s==owner]}
            for owner in sorted(selected)}

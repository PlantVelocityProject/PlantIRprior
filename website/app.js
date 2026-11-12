'use strict';

// Data browsers stay framework-independent; React islands use the bridge below.
const SPECIES = window.PLANTIR_RELEASES?.species || {
  ath: {name:'Arabidopsis thaliana', common:'Arabidopsis', events:2256, samples:146, assembly:'TAIR10', accession:'GCA_000001735.1', annotation:'Araport11'},
  gmx: {name:'Glycine max', common:'Soybean', events:2097, samples:61, assembly:'Glycine_max_v2.1', accession:'GCA_000004515.4', annotation:'Wm82.a2.v1'},
  zma: {name:'Zea mays', common:'Maize', events:1782, samples:175, assembly:'Zm-B73-REFERENCE-NAM-5.0', accession:'GCA_902167145.1', annotation:'Zm00001eb.1'},
  sly: {name:'Solanum lycopersicum', common:'Tomato', events:1330, samples:53, assembly:'SL4.0', accession:'GCA_000188115.5', annotation:'ITAG4.0'},
  osa: {name:'Oryza sativa', common:'Rice', events:1470, samples:248, assembly:'IRGSP-1.0', accession:'GCA_001433935.1', annotation:'RAP-DB'},
};
const $ = id => document.getElementById(id);
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const number = value => Number(value).toLocaleString('en-US');
const normalise = value => String(value).toLowerCase().replace(/[–−]/g,'-').replace(/,/g,'').trim();
const PAGE_SIZE = 50;
const RESOURCE_PAGES = ['resources','references','samples'];
const state = {page:'overview', species:'all', eventPage:0, samplePage:0, events:[], samples:[], data:null, view:null, routeToken:0};
const dataLoads = new Map();
const syncFilters = () => window.dispatchEvent(new Event('plantir:filters'));

function speciesCode(name) {
  return Object.keys(SPECIES).find(code=>SPECIES[code].name===name.replaceAll('_',' ')) || 'all';
}

function loadStudyData(view) {
  if (dataLoads.has(view.key)) return dataLoads.get(view.key);
  const loading = new Promise((resolve,reject) => {
    const script = document.createElement('script');
    script.src = view.file.path;
    script.onload = () => {
      const data = window.PLANTIR_DATASETS?.[view.key];
      if (!data?.events?.rows || !data?.samples?.rows) {
        dataLoads.delete(view.key);script.remove();reject(new Error('Version data are incomplete.'));return;
      }
      resolve(data);
    };
    script.onerror = () => {dataLoads.delete(view.key);script.remove();reject(new Error('Version data could not be loaded.'));};
    document.head.append(script);
  });
  dataLoads.set(view.key,loading);
  return loading;
}

function setStudyData(data) {
  state.data=data;
  state.events=data.events.rows.map((row,index)=>{
    const record=Object.fromEntries(data.events.columns.map((key,i)=>[key,row[i]]));
    return {...record,code:speciesCode(record.species),index,search:normalise(`${record.gene_id} ${record.intron_id} ${record.intron_uid} ${record.chromosome}:${record.start}-${record.end}`)};
  });
  state.samples=data.samples.rows.map((row,index)=>{
    const record=Object.fromEntries(data.samples.columns.map((key,i)=>[key,row[i]]));
    return {...record,code:speciesCode(record.Species),index,search:normalise(row.join(' '))};
  });
  const protocols=[...new Set(state.samples.map(row=>row.Protocol).filter(Boolean))].sort();
  $('sample-protocol').innerHTML='<option value="all">All protocols</option>'+protocols.map(p=>`<option value="${escapeHTML(p)}">${escapeHTML(p)}</option>`).join('');
}

function viewSuffix(code) {
  const query=new URLSearchParams();
  if(code!=='all')query.set('species',code);
  if(state.view?.record)query.set('release',state.view.record.key);
  return query.size ? `?${query}` : '';
}

function updateSpeciesURL(code) {
  state.species = code;
  history.replaceState(null,'',`#${state.page}${viewSuffix(code)}`);
}

function updateCatalogueContext() {
  if (!state.view) return;
  const code = $('species-filter').value;
  $('species-filter').classList.toggle('latin-selection',code!=='all');
  $('catalogue-dataset').innerHTML = window.PlantIRReleases.versionDetails(state.view,code,viewSuffix(code));
}

function updateResourceContext() {
  const code = state.species;
  const host = $(`${state.page}-dataset`);
  const open = host.querySelector('details')?.open;
  host.innerHTML = window.PlantIRReleases.versionDetails(state.view,code,viewSuffix(code),state.page);
  if (open) host.querySelector('details').open = true;
}

function matchedEvents() {
  const code = $('species-filter').value;
  const query = normalise($('ir-search').value);
  const overlap = $('overlap-filter').value;
  return state.events.filter(row => (code==='all'||row.code===code)
    && (overlap==='all'||row.exon_overlap_warning===overlap)
    && (!query||row.search.includes(query)));
}

function catalogueRow(row, showSpecies, mobile) {
  const species = showSpecies ? `<i class="latin-name">${escapeHTML(SPECIES[row.code]?.name || row.species)}</i>` : '';
  const coordinate = `<code class="coordinate">${escapeHTML(row.chromosome)}:${escapeHTML(row.start)}–${escapeHTML(row.end)}</code>`;
  const ratio = Number(row.IR_ratio).toFixed(3);
  const specificity = Number(row.IR_specificity).toFixed(3);
  const detailId = `ir-evidence-${mobile?'mobile':'table'}-${row.index}`;
  const disclosure = `<button type="button" class="table-action" data-open-event="${row.index}" aria-expanded="false" aria-controls="${detailId}" aria-label="View evidence for ${escapeHTML(row.intron_id)}">View evidence</button>`;
  if (mobile) {
    return `<li data-event="${row.index}"><div class="catalogue-mobile-row"><div><strong>${escapeHTML(row.intron_id)}</strong><div class="catalogue-event-meta">${coordinate}${species?`<span>${species}</span>`:''}</div></div>${disclosure}</div><p class="catalogue-metrics">IR ratio <b>${ratio}</b> · Specificity <b>${specificity}</b></p><div id="${detailId}" hidden></div></li>`;
  }
  return `<tr data-event="${row.index}">${showSpecies?`<td>${species}</td>`:''}<td><strong>${escapeHTML(row.gene_id)}</strong><small>${escapeHTML(row.intron_id)}</small></td>
    <td>${coordinate}</td><td class="numeric">${ratio}</td><td class="numeric">${specificity}</td><td class="numeric">${number(row.N_retained)}</td><td>${disclosure}</td></tr>
    <tr class="catalogue-detail-row" id="${detailId}" hidden><td colspan="${showSpecies?7:6}"></td></tr>`;
}

function renderCatalogue() {
  if (!state.data) return;
  const rows = matchedEvents();
  const pages = Math.max(1,Math.ceil(rows.length/PAGE_SIZE));
  state.eventPage = Math.max(0,Math.min(state.eventPage,pages-1));
  const offset = state.eventPage*PAGE_SIZE;
  const visible = rows.slice(offset,offset+PAGE_SIZE);
  const showSpecies = $('species-filter').value==='all';
  $('ir-species-heading').hidden = !showSpecies;
  $('ir-table-body').innerHTML = visible.map(row=>catalogueRow(row,showSpecies,false)).join('');
  $('ir-result-list').innerHTML = visible.map(row=>catalogueRow(row,showSpecies,true)).join('');
  $('catalogue-rows').hidden = rows.length===0;
  $('catalogue-empty').hidden = rows.length!==0;
  $('catalogue-pagination').hidden = pages===1;
  $('catalogue-filter-toggle').querySelector('span').textContent = $('overlap-filter').value==='all' ? 'Filters' : 'Filters (1)';
  $('ir-table-count').textContent = rows.length ? `Showing ${number(offset+1)}–${number(Math.min(offset+PAGE_SIZE,rows.length))} of ${number(rows.length)} IR events` : '0 matching IR events';
  $('event-page-number').textContent = `${state.eventPage+1} / ${pages}`;
  $('previous-page').disabled = state.eventPage===0;
  $('next-page').disabled = state.eventPage>=pages-1;
  $('export-events').disabled = rows.length===0;
}

function matchedSamples() {
  const code = $('sample-species').value;
  const query = normalise($('sample-search').value);
  const protocol = $('sample-protocol').value;
  return state.samples.filter(row => (code==='all'||row.code===code) && (protocol==='all'||row.Protocol===protocol) && (!query||row.search.includes(query)));
}

function sourceLink(accession,database) {
  const encoded = encodeURIComponent(accession);
  if (/^[SED]RR\d+$/.test(accession)) return `https://www.ncbi.nlm.nih.gov/sra/${encoded}`;
  if (/^CRR\d+$/.test(accession)) return `https://ngdc.cncb.ac.cn/gsa/browse/${encoded}`;
  if (/^PRJ[END]?[A-Z]\d+$/.test(accession)) return `https://www.ncbi.nlm.nih.gov/bioproject/${encoded}`;
  if (/^PRJCA\d+$/.test(accession)) return `https://ngdc.cncb.ac.cn/bioproject/browse/${encoded}`;
  return '';
}

function accessionCell(value,database) {
  const url = sourceLink(value,database);
  return url ? `<a class="identifier" href="${url}" target="_blank" rel="noopener">${escapeHTML(value)}</a>` : `<span class="identifier">${escapeHTML(value||'—')}</span>`;
}

function resourceFields(fields) {
  return `<dl class="resource-fields">${fields.map(([label,value])=>`<div><dt>${label}</dt><dd>${value}</dd></div>`).join('')}</dl>`;
}

function sampleRow(row, mobile) {
  const accession = accessionCell(row.Accession,row.Database);
  const species = `<i class="latin-name">${escapeHTML(row.Species)}</i>`;
  const tissue = escapeHTML(row.Tissue||'—');
  const treatment = escapeHTML(row.Treatment||'—');
  const protocol = escapeHTML(row.Protocol||'—');
  const project = accessionCell(row.BioProject,row.Database);
  if (mobile) {
    return `<li data-sample="${row.index}"><div class="sample-summary">${accession}${species}</div>
      <details><summary><span>${tissue} · ${protocol}</span><span class="catalogue-version-action">Details</span></summary>
        ${resourceFields([['Treatment',treatment],['BioProject',project]])}
      </details></li>`;
  }
  return `<tr data-sample="${row.index}"><td>${accession}</td><td>${species}</td><td>${tissue}</td><td>${treatment}</td><td>${protocol}</td><td>${project}</td></tr>`;
}

function renderSamples() {
  if (!state.data) return;
  $('sample-species').classList.toggle('latin-selection',$('sample-species').value!=='all');
  const rows = matchedSamples();
  const pages = Math.max(1,Math.ceil(rows.length/PAGE_SIZE));
  state.samplePage = Math.max(0,Math.min(state.samplePage,pages-1));
  const offset = state.samplePage*PAGE_SIZE;
  const visible = rows.slice(offset,offset+PAGE_SIZE);
  $('sample-table-body').innerHTML = visible.map(row=>sampleRow(row,false)).join('');
  $('sample-list').innerHTML = visible.map(row=>sampleRow(row,true)).join('');
  $('sample-rows').hidden = rows.length===0;
  $('sample-empty').hidden = rows.length!==0;
  $('sample-pagination').hidden = pages===1;
  $('sample-filter-toggle').querySelector('span').textContent = $('sample-protocol').value==='all' ? 'Filters' : 'Filters (1)';
  $('sample-table-count').textContent = rows.length ? `Showing ${number(offset+1)}–${number(offset+visible.length)} of ${number(rows.length)} source samples` : '0 matching samples';
  $('sample-page-number').textContent = `${state.samplePage+1} / ${pages}`;
  $('sample-previous').disabled = state.samplePage===0;
  $('sample-next').disabled = state.samplePage>=pages-1;
  $('export-samples').disabled = rows.length===0;
}

function renderResources(code) {
  const {index, downloadList, catalogueRecord, formatSize} = window.PlantIRReleases;
  const record = state.view.record;
  $('resource-bundle-panel').hidden = Boolean(record);
  $('resource-download-title').textContent = record ? 'Release files' : 'Download by species';
  $('resource-reference-link').href = `#references${viewSuffix(code)}`;
  const kinds = ['events','prior','samples'];
  if (record) {
    $('resource-downloads').innerHTML = downloadList(record,[...kinds,'evidence']);
  } else {
    const {samples,files} = index.snapshot;
    const codes = window.PlantIRReleases.speciesCodes();
    $('resource-bundle').href = files.bundle.path;
    $('resource-bundle-summary').textContent = `${number(codes.length)} plant species · ${number(samples)} samples · includes checksums`;
    $('resource-bundle-size').textContent = formatSize(files.bundle.bytes);
    const groups = (code==='all' ? codes : [code]).map(key=>{
      const release = catalogueRecord(state.view,key);
      return `<li class="resource-download-group"><div><h3><a href="#catalogue${viewSuffix(key)}"><i class="latin-name">${escapeHTML(SPECIES[key].name)}</i></a></h3>
        <p>${number(release.events)} IR events · ${number(release.samples)} samples</p></div>${downloadList(release,kinds)}</li>`;
    });
    $('resource-downloads').innerHTML = `<ul class="resource-download-groups" role="list">${groups.join('')}</ul>`;
  }
  $('resource-content').hidden = false;
}

function renderReferences(code) {
  const {speciesFor,speciesCodes} = window.PlantIRReleases;
  const species = speciesFor(state.view);
  const references = (code==='all' ? speciesCodes() : [code]).map(key=>{
    const s = species[key];
    const name = `<i class="latin-name">${escapeHTML(s.name)}</i>${key==='osa'?' Japonica':''}`;
    const accession = s.accession ? `<a class="identifier" href="https://www.ncbi.nlm.nih.gov/datasets/genome/${encodeURIComponent(s.accession)}/" target="_blank" rel="noopener">${escapeHTML(s.accession)}</a>` : 'Not recorded';
    return {name, fields:[
      ['Assembly',`<code>${escapeHTML(s.assembly)}</code>`],
      ['Assembly accession',accession],
      ['Annotation',`<code>${escapeHTML(s.annotation)}</code>`],
    ]};
  });
  $('reference-table-body').innerHTML = references.map(({name,fields})=>`<tr><td>${name}</td>${fields.map(([,value])=>`<td>${value}</td>`).join('')}</tr>`).join('');
  $('reference-list').innerHTML = references.map(({name,fields})=>`<li><h3>${name}</h3>${resourceFields(fields)}</li>`).join('');
  $('reference-content').hidden = false;
}

function eventEvidence(row) {
  const release = window.PlantIRReleases.catalogueRecord(state.view,row.code);
  const items = [
    ['IR ratio',Number(row.IR_ratio).toFixed(3)], ['IR specificity',Number(row.IR_specificity).toFixed(3)],
    ['Retained reads',number(row.N_retained)], ['Spliced reads',number(row.N_spliced)],
    ['Retention-specific reads',number(row.N_specific)], ['Informative reads',number(row.N_informative)],
  ];
  const provenance = release ? `<a href="${window.PlantIRReleases.url('release',release)}">Release ${release.generation}</a> · ${escapeHTML(release.reference_id)} / ${escapeHTML(release.annotation_id)} · <code>${escapeHTML(release.release_id)}</code>` : 'Release metadata unavailable';
  return `<div class="catalogue-evidence"><p><i class="latin-name">${escapeHTML(SPECIES[row.code]?.name || row.species)}</i> · ${provenance}</p><p><code>${escapeHTML(row.intron_uid)}</code></p>
    <dl class="event-evidence">${items.map(([label,value])=>`<div><dt>${label}</dt><dd>${value}</dd></div>`).join('')}</dl>
    <p><strong>Strand:</strong> ${escapeHTML(row.strand)} · <strong>Exon overlap:</strong> ${row.exon_overlap_warning==='yes'?'Warning':'No warning'}</p><p><strong>Supporting transcripts:</strong> <code>${escapeHTML(row.supporting_transcripts||'Not recorded')}</code></p></div>`;
}

function setEventDisclosure(button, expanded, content='') {
  const row = state.events[Number(button.dataset.openEvent)];
  const panel = $(button.getAttribute('aria-controls'));
  const label = expanded ? 'Hide evidence' : 'View evidence';
  button.textContent = label;
  button.setAttribute('aria-expanded',String(expanded));
  button.setAttribute('aria-label',`${label} for ${row.intron_id}`);
  panel.hidden = !expanded;
  const body = panel.tagName==='TR' ? panel.cells[0] : panel;
  body.innerHTML = content;
}

function toggleEventEvidence(button) {
  const row = state.events[Number(button.dataset.openEvent)];
  if (!row) return;
  const open = button.getAttribute('aria-expanded')==='false';
  const results = $('catalogue-results');
  results.querySelectorAll('[data-open-event][aria-expanded="true"]').forEach(control=>setEventDisclosure(control,false));
  if (open) {
    const content = eventEvidence(row);
    results.querySelectorAll(`[data-open-event="${row.index}"]`).forEach(control=>setEventDisclosure(control,true,content));
  }
}

function downloadCSV(rows,columns,filename) {
  const quote = value => `"${String(value??'').replaceAll('"','""')}"`;
  const content = '\uFEFF' + [columns.map(quote).join(','),...rows.map(row=>columns.map(key=>quote(row[key])).join(','))].join('\r\n')+'\r\n';
  const url = URL.createObjectURL(new Blob([content],{type:'text/csv;charset=utf-8'}));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}

function exportVersion(code) {
  const record = window.PlantIRReleases.catalogueRecord(state.view,code);
  return record ? `${record.species}-${record.release_id}` : `snapshot-${window.PLANTIR_RELEASES.snapshot.id}`;
}

async function route() {
  const [requested,query=''] = location.hash.slice(1).split('?');
  if(requested==='changelog'){location.replace('#releases');return;}
  // Section anchors inside Releases remain on their parent page.
  if(requested==='release-history') {if(state.page!=='releases'){location.hash='releases';}else document.getElementById('release-history')?.scrollIntoView();return;}
  const page = document.getElementById(`page-${requested}`) ? requested : 'overview';
  const params=new URLSearchParams(query);
  const previousPage=state.page; const token=++state.routeToken;
  state.page=page; state.data=null; state.view=null;
  let code=Object.hasOwn(SPECIES,params.get('species')) ? params.get('species') : 'all';
  document.querySelectorAll('.page').forEach(section=>section.hidden=section.id!==`page-${page}`);
  document.title=`${{overview:'Plant intron-retention resources',catalogue:'Browse IR priors',resources:'All downloads',references:'Genomes & annotations',samples:'Source samples',methods:'Methods',releases:'Releases',release:'Release details'}[page]} | PlantIR Prior`;
  if(previousPage!==page){window.scrollTo({top:0,behavior:'instant'});$('main-content').focus({preventScroll:true});}
  window.dispatchEvent(new CustomEvent('plantir:route',{detail:{page,species:code}}));
  if(page==='releases'||page==='release'){window.PlantIRReleases.render(page,params);return;}
  if(page!=='catalogue'&&!RESOURCE_PAGES.includes(page))return;
  const eventPage=page==='catalogue';
  if(eventPage){
    state.eventPage=0;
    $('ir-search').value=params.get('q')||'';
    $('overlap-filter').value='all';
    $('catalogue-filter-toggle').querySelector('span').textContent='Filters';
    $('catalogue-filter-toggle').setAttribute('aria-expanded','false');
    for(const id of ['catalogue-advanced','catalogue-rows','catalogue-empty','catalogue-pagination'])$(id).hidden=true;
    for(const id of ['catalogue-dataset','ir-table-body','ir-result-list'])$(id).replaceChildren();
    $('ir-table-count').textContent='Loading version data…';
    $('export-events').disabled=true;
  }
  if(page==='samples'){
    $('sample-search').value=params.get('q')||'';
    $('sample-protocol').value='all';
    $('sample-protocol').disabled=true;
    $('sample-filter-toggle').querySelector('span').textContent='Filters';
    $('sample-filter-toggle').setAttribute('aria-expanded','false');
    for(const id of ['sample-advanced','sample-rows','sample-empty','sample-pagination'])$(id).hidden=true;
    $('sample-table-count').textContent='Loading version data…';
    $('export-samples').disabled=true;
    state.samplePage=0;
  }
  try {
    state.view=window.PlantIRReleases.selection(params);
    if(state.view.record)code=state.view.record.species;
    state.species=code;
    for(const id of ['species-filter','sample-species']){
      $(id).disabled=Boolean(state.view.record);
      $(id).value=code;
    }
    syncFilters();
    if(eventPage)updateCatalogueContext();
    else {
      $(`${page}-dataset`).replaceChildren();
      updateResourceContext();
    }
    if(page==='resources'){renderResources(code);return;}
    if(page==='references'){renderReferences(code);return;}
    if(!eventPage)$('sample-content').hidden=false;
    const data=await loadStudyData(state.view);
    if(state.routeToken!==token)return;
    setStudyData(data);
    if(!eventPage){
      $('sample-protocol').disabled=false;
      syncFilters();
    }
    (eventPage?renderCatalogue:renderSamples)();
  } catch(error) {
    if(state.routeToken!==token)return;
    state.data=null;
    state.view=null;
    $(`${page}-dataset`).innerHTML=`<div class="dataset-banner"><span role="alert">${escapeHTML(error.message)}</span><a href="#releases">Choose a release</a><button type="button" class="table-action" data-retry>Retry</button></div>`;
    if(eventPage){
      $('ir-table-count').textContent='Version unavailable';
      syncFilters();
      return;
    }
    const content = {resources:'resource-content',references:'reference-content',samples:'sample-content'};
    $(content[page]).hidden=true;
  }
}

document.querySelectorAll('.species-select').forEach(select=>{
  select.innerHTML = '<option value="all">All species</option>'+Object.entries(SPECIES).map(([code,s])=>`<option value="${code}" class="latin-name">${s.name}</option>`).join('');
});
$('home-ir-search').addEventListener('input',event=>event.currentTarget.setCustomValidity(''));
$('home-search').addEventListener('submit',event=>{
  event.preventDefault();
  const input = $('home-ir-search');
  const query = input.value.trim();
  if (!query) {
    input.setCustomValidity('Enter a gene, intron or genomic interval.');
    input.reportValidity();
    return;
  }
  location.hash = `catalogue?${new URLSearchParams({q:query})}`;
});
$('ir-search').addEventListener('input',()=>{state.eventPage=0;renderCatalogue();});
$('overlap-filter').addEventListener('change',()=>{state.eventPage=0;renderCatalogue();});
$('catalogue-filter-toggle').addEventListener('click',event=>{
  const panel = $('catalogue-advanced');
  panel.hidden = !panel.hidden;
  event.currentTarget.setAttribute('aria-expanded',String(!panel.hidden));
});
$('species-filter').addEventListener('change',()=>{
  if(!state.view)return;
  state.eventPage=0;updateSpeciesURL($('species-filter').value);updateCatalogueContext();renderCatalogue();
});
$('page-catalogue').addEventListener('click',event=>{
  if(!state.view)return;
  const disclosure=event.target.closest('[data-open-event]');
  if(disclosure)toggleEventEvidence(disclosure);
  if(event.target.closest('[data-reset-catalogue]')){
    $('ir-search').value='';$('species-filter').value=state.view.record?.species||'all';$('overlap-filter').value='all';
    state.eventPage=0;updateSpeciesURL($('species-filter').value);updateCatalogueContext();renderCatalogue();
    syncFilters();
    $('ir-search').focus({preventScroll:true});
  }
});
$('previous-page').addEventListener('click',()=>{state.eventPage--;renderCatalogue();});
$('next-page').addEventListener('click',()=>{state.eventPage++;renderCatalogue();});
$('export-events').addEventListener('click',()=>{
  if (!state.data) return;
  const version=exportVersion($('species-filter').value);
  downloadCSV(matchedEvents(),state.data.events.columns,`plantir-${version}-ir-events.csv`);
});
for (const id of ['sample-search','sample-species','sample-protocol']) {
  $(id).addEventListener(id==='sample-search'?'input':'change',()=>{
    if(!state.view)return;
    state.samplePage=0;
    if (id==='sample-species'){
      updateSpeciesURL($('sample-species').value);
      updateResourceContext();
    }
    renderSamples();
  });
}
$('sample-filter-toggle').addEventListener('click',event=>{
  const panel=$('sample-advanced');
  panel.hidden=!panel.hidden;
  event.currentTarget.setAttribute('aria-expanded',String(!panel.hidden));
});
$('page-samples').addEventListener('click',event=>{
  if(!state.view||!event.target.closest('[data-reset-samples]'))return;
  $('sample-search').value='';
  $('sample-species').value=state.view.record?.species||'all';
  $('sample-protocol').value='all';
  state.samplePage=0;
  updateSpeciesURL($('sample-species').value);
  updateResourceContext();
  renderSamples();
  syncFilters();
  $('sample-search').focus({preventScroll:true});
});
$('sample-previous').addEventListener('click',()=>{state.samplePage--;renderSamples();});
$('sample-next').addEventListener('click',()=>{state.samplePage++;renderSamples();});
$('export-samples').addEventListener('click',()=>{
  if (!state.data) return;
  const version=exportVersion($('sample-species').value);
  downloadCSV(matchedSamples(),state.data.samples.columns,`plantir-${version}-samples.csv`);
});
document.addEventListener('click',event=>{
  if (event.target.closest('[data-retry]')) route();
});
window.addEventListener('hashchange',route);
const published = window.PLANTIR_RELEASES;
const speciesCodes = window.PlantIRReleases?.speciesCodes() || [];
window.PlantIR = {
  species: SPECIES,
  speciesOrder: speciesCodes.length ? speciesCodes : Object.keys(SPECIES),
  downloads:Object.fromEntries(Object.keys(SPECIES).map(code=>[code,published?.records[published.latest[code]]?.files.events.path || '#releases'])),
  resourceHref:href=>state.view && RESOURCE_PAGES.includes(state.page) && RESOURCE_PAGES.includes(href.slice(1))
    ? href+viewSuffix(state.species) : href,
};
if(published){
  $('home-species-count').textContent=Object.keys(SPECIES).length;
  $('home-samples-count').textContent=number(published.snapshot.samples);
  $('home-events-count').textContent=number(published.snapshot.events);
  const date = $('home-snapshot-date');
  date.dateTime=published.updated_at;
  date.textContent=window.PlantIRReleases.formatDate(published.updated_at);
  $('home-snapshot-link').hidden=false;
  document.querySelector('meta[name="description"]').content=`Browse ${number(published.snapshot.events)} long-read-supported intron-retention priors across ${Object.keys(SPECIES).length} plant species.`;
}
route();

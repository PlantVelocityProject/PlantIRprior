/* Version navigation shared by releases, catalogue, samples and downloads. */
(() => {
  'use strict';
  const index = window.PLANTIR_RELEASES;
  const escape = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const number = value => Number(value).toLocaleString('en-US');
  const date = value => new Intl.DateTimeFormat('en-GB',{day:'2-digit',month:'short',year:'numeric',timeZone:'UTC'}).format(new Date(value));
  const size = bytes => bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes/1024).toFixed(1)} KB` : `${(bytes/1048576).toFixed(1)} MB`;
  const order = ['ath','gmx','zma','sly','osa'];
  const codes = () => [...new Set([...order.filter(c=>index?.latest[c]),...Object.keys(index?.latest||{})])];
  const url = (page, record) => `#${page}?species=${encodeURIComponent(record.species)}&release=${encodeURIComponent(record.key)}`;
  const fileLink = (file, label, css='') => `<a class="${css}" href="${escape(file.path)}" download>${escape(label||file.label)}</a>`;
  const downloadTypes = {
    prior: ['PlantVelo prior', 'TSV', 'Five columns for PlantVelo counting'],
    events: ['High-confidence IR events', 'CSV', 'IR ratios, read counts and intron annotations'],
    samples: ['Included samples', 'CSV', 'The cohort used to generate this release'],
    evidence: ['Complete intron evidence', 'TSV.gz', 'All evaluated introns and their classifications'],
  };
  const botanical = code => `<img class="release-botanical" src="assets/${escape(index.species[code].art)}.webp" alt="" width="64" height="64">`;
  const current = record => index.latest[record.species] === record.key;
  const status = record => `<span class="release-status${current(record)?'':' is-archived'}">${current(record)?'Current':'Archived'}</span>`;
  const errorHTML = message => `<div class="release-error" role="alert"><h1>Release unavailable</h1><p>${escape(message)}</p><a class="button secondary" href="#releases">View available releases</a></div>`;

  function menuGroups() {
    if (!index) return [];
    const records = Object.values(index.records);
    return codes().map(code => ({
      species: index.species[code].common,
      versions: records.filter(record => record.species === code)
        .sort((a,b) => b.generation - a.generation || b.created_at.localeCompare(a.created_at))
        .map(record => ({
          href: url('release', record),
          generation: record.generation,
          current: current(record),
          createdAt: record.created_at,
          dateLabel: date(record.created_at),
        })),
    }));
  }

  function selection(params) {
    if (!index) throw new Error('Release information could not be loaded. Reload this page to try again.');
    if (params.has('dataset')) throw new Error('This legacy dataset has been retired. Choose a formal release.');
    if (params.has('release')) {
      const record = index.records[params.get('release')];
      if (!record) throw new Error('This exact release is not in the published catalogue. Choose an available release.');
      if (params.has('species') && params.get('species') !== record.species) throw new Error('The selected species does not match this release.');
      return {key:record.key,file:record.files.browse,record};
    }
    return {key:index.snapshot.key,file:index.snapshot.files.browse,record:null};
  }

  function speciesFor(view) {
    const result = structuredClone(index.species);
    if (view.record) {
      const r = view.record; const s = result[r.species];
      if (s.assembly !== r.reference_id || s.annotation !== r.annotation_id) s.accession = '';
      Object.assign(s,{assembly:r.reference_id,annotation:r.annotation_id,events:r.events,samples:r.samples});
    }
    return result;
  }

  function catalogueRecord(view, code) {
    return view.record || index.records[index.latest[code]];
  }

  function versionDetails(view, code, suffix, page='catalogue') {
    const compact = page !== 'catalogue';
    const record = catalogueRecord(view, code);
    const records = record ? [record] : codes().map(c => catalogueRecord(view, c));
    const generated = value => `<time datetime="${escape(value)}">${date(value)}</time>`;
    const reference = r => `${escape(r.reference_id)} / ${escape(r.annotation_id)}`;
    const snapshot = `Snapshot <code>${escape(index.snapshot.id)}</code>`;
    const identity = record
      ? `${status(record)}<strong><i class="latin-name">${escape(index.species[record.species].name)}</i> · Release ${record.generation}</strong>${compact?'':`<small>${reference(record)} · ${generated(record.created_at)}</small>`}`
      : `<span class="release-status">Current snapshot</span><strong>${generated(index.updated_at)}</strong>${compact?'':`<small>${snapshot}</small>`}`;
    const versions = records.map(r => `<li><a href="${url('release', r)}"><i class="latin-name">${escape(index.species[r.species].name)}</i> · Release ${r.generation}</a><span>${reference(r)}${compact?` · ${generated(r.created_at)}`:''} · <code>${escape(r.release_id)}</code></span></li>`).join('');
    const related = [
      ['samples', 'Source samples'],
      ['references', compact?'Genomes &amp; annotations':'Genome &amp; annotation'],
      ['resources', 'Downloads'],
    ].filter(([target])=>target!==page).map(([target,label])=>`<a class="quiet-link" href="#${target}${escape(suffix)}">${label}</a>`).join('');
    const releaseLink = record && !current(record)
      ? `<a class="quiet-link" href="${url('release', index.records[index.latest[record.species]])}">View latest release</a>`
      : compact && !record ? '<a class="quiet-link" href="#releases">Release information</a>' : '';
    return `<details class="catalogue-provenance"><summary><span class="catalogue-version-context">${identity}</span><span class="catalogue-version-action">Version details</span></summary>
      <div class="catalogue-version-details">${compact&&!record?`<p class="resource-snapshot-id">${snapshot}</p>`:''}<ul>${versions}</ul>
        <div class="text-links">${related}${releaseLink}</div>
      </div></details>`;
  }

  function downloadList(record, keys, descriptions=false) {
    return `<ul class="release-download-list" role="list">${keys.map(key=>{
      const [label,format,description] = downloadTypes[key];
      const file = record.files[key];
      return `<li><div>${fileLink(file,label)}${descriptions?`<small>${description}</small>`:''}</div><span class="release-download-meta">${format} · ${size(file.bytes)}</span></li>`;
    }).join('')}</ul>`;
  }

  function overview(params) {
    const filter = params.get('species') || 'all';
    const visible = codes().filter(c=>filter==='all'||c===filter);
    const history = Object.values(index.records).filter(r=>filter==='all'||r.species===filter)
      .sort((a,b)=>b.created_at.localeCompare(a.created_at)||b.generation-a.generation||codes().indexOf(a.species)-codes().indexOf(b.species));
    return `<section class="release-snapshot" aria-labelledby="snapshot-title">
        <div class="snapshot-intro"><div><span class="release-status">Current snapshot</span><h2 id="snapshot-title">Five species. One reproducible resource.</h2><p>Each species follows its own release history. This download brings their latest versions together.</p></div><div class="snapshot-specimens" aria-hidden="true">${codes().map(botanical).join('')}</div></div>
        <div class="snapshot-bottom"><dl class="snapshot-counts"><div><dt>Plant species</dt><dd>${codes().length}</dd></div><div><dt>Included samples</dt><dd>${number(index.snapshot.samples)}</dd></div><div><dt>High-confidence IR events</dt><dd>${number(index.snapshot.events)}</dd></div></dl><div class="snapshot-actions">${fileLink(index.snapshot.files.bundle,'Download snapshot','button primary')}${fileLink(index.snapshot.files.manifest,'Version manifest','quiet-link')}</div></div>
        <p class="snapshot-updated">Website data updated <time datetime="${escape(index.updated_at)}">${date(index.updated_at)}</time> · Snapshot <span>${escape(index.snapshot.id)}</span></p>
      </section>
      <section class="release-register" aria-labelledby="species-releases-title"><div class="section-heading"><div><h2 id="species-releases-title">Latest by species</h2></div><div class="release-filter"><label for="release-species">Species</label><select id="release-species" data-filter-select="species"><option value="all">All species</option>${codes().map(c=>`<option value="${c}"${filter===c?' selected':''}>${escape(index.species[c].name)}</option>`).join('')}</select></div></div>
      <table class="release-table"><caption class="sr-only">Latest formal release for each species</caption><thead><tr><th>Species</th><th>Version</th><th>Genome / annotation</th><th class="numeric">Samples</th><th class="numeric">IR events</th><th><span class="sr-only">Release details</span></th></tr></thead><tbody>${visible.map(code=>{
        const r=index.records[index.latest[code]],s=index.species[code];
        return `<tr><td data-label="Species"><div class="release-species">${botanical(code)}<div><strong>${escape(s.common)}</strong><i class="latin-name">${escape(s.name)}</i></div></div></td><td data-label="Version"><a class="release-version" href="${url('release',r)}">Release ${r.generation}</a><small>Generated ${date(r.created_at)}</small></td><td data-label="Genome / annotation" class="release-reference"><span>${escape(r.reference_id)}</span><small>${escape(r.annotation_id)}</small></td><td data-label="Samples" class="numeric">${number(r.samples)}</td><td data-label="IR events" class="numeric">${number(r.events)}</td><td class="release-row-action"><a class="button secondary" href="${url('release',r)}">View release</a></td></tr>`;
      }).join('')}</tbody></table></section>
      <section id="release-history" class="release-history" aria-labelledby="history-title"><div class="section-heading"><div><h2 id="history-title">Release history</h2></div></div><ol class="release-history-list">${history.map(r=>`<li><span class="history-node" aria-hidden="true"></span><div><div class="history-title"><a href="${url('release',r)}">${escape(index.species[r.species].common)} <span>Release ${r.generation}</span></a>${status(r)}</div><p>${r.changes.kind==='initial'?'Initial release':`${number(r.changes.samples_added)} samples added; ${number(r.changes.events_added)} IR events added; ${number(r.changes.events_removed)} removed.`}</p><small>Generated ${date(r.created_at)} · ${escape(r.reference_id)} / ${escape(r.annotation_id)}</small></div><a class="quiet-link" href="${url('release',r)}" aria-label="View ${escape(index.species[r.species].common)} release ${r.generation}">Details</a></li>`).join('')}</ol></section>
      `;
  }

  function changes(r) {
    const c = r.changes;
    if (c.kind === 'initial') {
      return `<p><strong>Initial release.</strong> ${number(r.events)} high-confidence IR events from ${number(r.samples)} included long-read samples.</p>`;
    }
    const previous = index.records[c.previous];
    const groups = [
      ['Samples', [['Added',c.samples_added],['Removed',c.samples_removed],['Versions revised',c.samples_revised]]],
      ['IR events', [['Added',c.events_added],['Removed',c.events_removed],['With updated evidence',c.evidence_changed]]],
    ];
    return `<p>Compared with <a href="${url('release',previous)}">Release ${previous.generation}</a> on the same reference and annotation.</p>
      <div class="release-change-groups">${groups.map(([title,rows])=>`<section class="release-change-group"><h3>${title}</h3><dl class="release-change-counts">${rows.map(([label,count])=>`<div><dt>${label}</dt><dd>${number(count)}</dd></div>`).join('')}</dl></section>`).join('')}</div>`;
  }

  function detail(params) {
    const r = selection(params).record;
    if (!r) return errorHTML('Select a species release from the release register.');
    const s = index.species[r.species];
    document.title=`${s.common} · Release ${r.generation} | PlantIR Prior`;
    return `<div class="release-detail-heading">
        <div><h1 id="release-title">${escape(s.common)} <span>Release ${r.generation}</span></h1><p class="latin-name">${escape(s.name)}</p>
          <div class="release-detail-meta">${status(r)}<time datetime="${escape(r.created_at)}" title="${escape(r.created_at)}">${date(r.created_at)}</time><span>${escape(r.reference_id)} / ${escape(r.annotation_id)}</span>${current(r)?'':`<a href="${url('release',index.records[index.latest[r.species]])}">View latest release</a>`}</div>
          <div class="release-id-line"><code id="release-id">${escape(r.release_id)}</code><button type="button" class="table-action" data-copy-release="${escape(r.release_id)}">Copy ID</button><span id="release-copy-status" role="status" aria-live="polite"></span></div>
        </div>${botanical(r.species)}
      </div>
      <section class="release-detail-section" aria-labelledby="coverage-title"><h2 id="coverage-title">Evidence in this release</h2><dl class="release-evidence-counts"><div><dt>High-confidence IR events</dt><dd>${number(r.events)}</dd></div><div><dt>Included samples</dt><dd>${number(r.samples)}</dd></div><div><dt>Introns evaluated</dt><dd>${number(r.intron_rows)}</dd></div></dl><div class="text-links"><a class="button primary" href="${url('catalogue',r)}">Browse IR events</a><a class="button secondary" href="${url('samples',r)}">View included samples</a></div></section>
      <section class="release-detail-section" aria-labelledby="changes-title"><h2 id="changes-title">What changed</h2>${changes(r)}</section>
      <section class="release-detail-section" aria-labelledby="downloads-title"><h2 id="downloads-title">Download this version</h2>
        ${downloadList(r,['prior','events','samples','evidence'],true)}
      </section>
      `;
  }

  function render(page,params) {
    const host=document.getElementById(page==='release'?'release-detail-ui':'releases-ui');
    if (!host) return;
    try { if (!index) throw new Error('Release metadata could not be loaded. Reload this page to try again.'); host.innerHTML=page==='release'?detail(params):overview(params); }
    catch(error) { host.innerHTML=errorHTML(error.message); }
    const filter=document.getElementById('release-species');
    if(filter)filter.addEventListener('change',()=>{location.hash=`releases${filter.value==='all'?'':`?species=${filter.value}`}`;});
    if(page==='releases')window.dispatchEvent(new Event('plantir:filters'));
    host.querySelector('[data-copy-release]')?.addEventListener('click',async event=>{
      const text=event.currentTarget.dataset.copyRelease;
      const copyStatus=host.querySelector('#release-copy-status');
      try {
        if(navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
        else {
          const range=document.createRange();range.selectNodeContents(host.querySelector('#release-id'));
          const selected=window.getSelection(); selected.removeAllRanges(); selected.addRange(range);
          if(!document.execCommand('copy'))throw new Error('manual');
        }
        copyStatus.textContent='Copied';
      } catch { copyStatus.textContent='Select and copy the ID above.'; }
    });
  }
  window.PlantIRReleases={index,selection,speciesFor,versionDetails,catalogueRecord,downloadList,render,url,menuGroups,speciesCodes:codes,formatDate:date,formatSize:size};
})();

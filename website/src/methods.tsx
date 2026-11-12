import * as React from 'react'
import originalArtwork from '../assets/methods/overview.svg?raw'
import agentArtwork from '../assets/methods/agent-update.svg?raw'
import {figureModules,figureViewport} from './methods-figure'
import {agentModules,agentViewport,agentRelations,agentConnections} from './methods-agent-figure'
import {MethodsWorkflow} from './methods-workflow'

const agentMotion={relations:agentRelations,connections:agentConnections,surfaceId:'agent-methods-surface',countRows:{}}

export function Methods(){
 const [active,setActive]=React.useState<string|null>(null)
 return <>
  <div className="page-heading methods-page-heading"><h1 id="methods-title">Methods</h1></div>
  <section aria-labelledby="methods-evidence-title">
   <div className="methods-section-heading"><h2 id="methods-evidence-title">High-confidence IR prior construction</h2>
    <p>How long-read evidence becomes a species-specific IR prior.</p></div>
   <MethodsWorkflow id="methods-workflow" label="PlantIR method workflow" artwork={originalArtwork}
    modules={figureModules} viewport={figureViewport} active={active} setActive={setActive}/>
  </section>
  <section className="methods-agent-section" aria-labelledby="methods-agent-title">
   <div className="methods-section-heading"><h2 id="methods-agent-title">Agent-assisted IR prior updates</h2>
    <p>From dataset discovery to reviewed, versioned IR priors.</p></div>
   <MethodsWorkflow id="methods-agent-workflow" label="Agent-assisted IR update workflow from Fig1 C" artwork={agentArtwork}
    modules={agentModules} viewport={agentViewport} active={active} setActive={setActive} motion={agentMotion}/>
  </section>
 </>
}

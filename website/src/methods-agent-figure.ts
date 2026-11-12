import type {FigureModule} from './methods-figure'
import type {MethodRelation, MethodConnection} from './methods-motion'

// Measured in Fig1 C's original PDF coordinate space; no reconstructed arrows.
export const agentViewport={x:31,y:564,width:554,height:167}
export const agentModules:FigureModule[]=[
 {id:'agent-plan',title:'Agent: plan, monitor and report',bounds:[39.72,600.11,77.59,77.59],
  description:'The agent plans the update, monitors execution and reports results. Status and execution feedback return from each stage to guide the next action.'},
 {id:'agent-discover',title:'Discover & curate datasets',bounds:[131.93,610.59,79.93,62.75],
  description:'Discover relevant long-read sequencing datasets and curate their sample metadata before retrieval and alignment.'},
 {id:'agent-align',title:'Retrieve & align reads',bounds:[222.71,610.59,79.93,62.75],
  description:'Retrieve the curated reads and align them to the matching reference genome, producing aligned evidence for IR quantification.'},
 {id:'agent-quantify',title:'Quantify & validate IR evidence',bounds:[313.34,610.59,79.93,62.75],
  description:'Quantify retained and spliced read–intron evidence and validate the results before adding them to the pooled intron counts.'},
 {id:'agent-pool',title:'Incrementally pool intron counts',bounds:[404.04,610.59,79.93,62.75],
  description:'Merge validated evidence into the existing intron counts to update the pooled evidence incrementally. Status is reported back to the agent.'},
 {id:'agent-classify',title:'Classify & release IR priors',bounds:[494.73,610.96,79.93,62.75],
  description:'Classify IR events from the updated evidence and prepare versioned IR priors. Updates are published manually after review.'},
 {id:'agent-feedback',title:'Status & execution feedback',bounds:[257,569.2,103,13],
  description:'Each execution stage sends its status back to the agent. The feedback loop supports monitoring and reporting throughout the update.'},
 {id:'agent-review',title:'Manual update upon review',bounds:[338.63,680.23,117.58,14.53],
  description:'A human reviews the update before manually publishing the IR priors. Agent-assisted execution preserves this manual review and publication step.'},
 {id:'agent-versioned',title:'Versioned IR priors',bounds:[480.65,680.23,94.13,14.53],
  description:'Reviewed updates produce versioned IR priors that are delivered to PlantVelocity and the web resource.'},
 {id:'agent-plantvelocity',title:'PlantVelocity',bounds:[429.46,710.28,67.71,14.54],
  description:'PlantVelocity uses the versioned IR priors as evidence for plant single-cell RNA velocity analysis.'},
 {id:'agent-web',title:'Web resource',bounds:[505.43,710.28,69.36,14.54],
  description:'The web resource provides the released IR priors, their versions and associated downloads for browsing and reuse.'},
 {id:'agent-provenance',title:'Provenance, versioning and reproducibility',bounds:[233,716,149,12],
  description:'Provenance, versioning and reproducibility span the full update, connecting source datasets and execution results to the released IR priors.'},
]

export const agentConnections:Record<string,MethodConnection>={
 plan:{paths:[588],head:589},
 discovery:{paths:[592],head:593},
 alignment:{paths:[594],head:595},
 evidence:{paths:[596],head:597},
 pooling:{paths:[598],head:599},
 review:{paths:[600]},
 'feedback-bus':{paths:[587],reverse:true},
 'feedback-agent':{paths:[590],head:591},
 'feedback-discover':{paths:[601],head:602},
 'feedback-align':{paths:[603],head:604},
 'feedback-quantify':{paths:[605],head:606},
 'feedback-pool':{paths:[607],head:608},
 'feedback-classify':{paths:[609],head:610},
 outputs:{paths:[577,578,579,581],heads:[580,582]},
}

const stages=['agent-discover','agent-align','agent-quantify','agent-pool','agent-classify']
const feedback=['feedback-discover','feedback-align','feedback-quantify','feedback-pool','feedback-classify','feedback-bus','feedback-agent']
export const agentRelations:Record<string,MethodRelation>={
 'agent-plan':{related:[...stages,'agent-feedback'],connections:['plan',...feedback],counts:[],color:'#744c88'},
 'agent-discover':{related:['agent-plan','agent-align','agent-feedback'],connections:['plan','discovery','feedback-discover','feedback-bus','feedback-agent'],counts:[]},
 'agent-align':{related:['agent-discover','agent-quantify','agent-feedback'],connections:['discovery','alignment','feedback-align','feedback-bus','feedback-agent'],counts:[]},
 'agent-quantify':{related:['agent-align','agent-pool','agent-feedback'],connections:['alignment','evidence','feedback-quantify','feedback-bus','feedback-agent'],counts:[]},
 'agent-pool':{related:['agent-quantify','agent-classify','agent-feedback'],connections:['evidence','pooling','feedback-pool','feedback-bus','feedback-agent'],counts:[]},
 'agent-classify':{related:['agent-pool','agent-review','agent-versioned','agent-feedback'],connections:['pooling','feedback-classify','feedback-bus','feedback-agent'],counts:[]},
 'agent-feedback':{related:['agent-plan',...stages],connections:feedback,counts:[],color:'#744c88'},
 'agent-review':{related:['agent-quantify','agent-pool','agent-classify','agent-versioned'],connections:['review'],counts:[],color:'#956020'},
 'agent-versioned':{related:['agent-review','agent-classify','agent-plantvelocity','agent-web'],connections:['outputs'],counts:[],color:'#427e59'},
 'agent-plantvelocity':{related:['agent-versioned'],connections:['outputs'],counts:[],color:'#427e59'},
 'agent-web':{related:['agent-versioned'],connections:['outputs'],counts:[],color:'#427e59'},
 'agent-provenance':{related:['agent-plan',...stages,'agent-review','agent-versioned'],connections:[],counts:[]},
}

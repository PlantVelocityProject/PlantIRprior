/** Immediate relationships in the published Methods figure, not a pipeline simulator. */
export type Count = 'retained' | 'spliced' | 'specific' | 'informative'
export type MethodRelation = {
  related: string[]
  connections: string[]
  counts: Count[]
  color?: string
  excluded?: boolean
}
export type MethodConnection = {paths:number[]; head?:number; heads?:number[]; reverse?:boolean}

// These indices refer to direct children of #methods-surface1 in the immutable
// PDF-derived artwork. Both the shaft and the original arrowhead are reused.
export const methodConnections: Record<string, MethodConnection> = {
  'iso-bam': {paths: [5, 6, 7], head: 8},
  'flep-bam': {paths: [9], head: 10},
  'ont-bam': {paths: [11, 12, 13], head: 14},
  evidence: {paths: [38], head: 39},
  pooling: {paths: [16], head: 17},
  merge: {paths: [25], head: 26},
  criteria: {paths: [28], head: 29},
  prior: {paths: [40], head: 41},
}

export const methodRelations: Record<string, MethodRelation> = {
  'iso-seq': {related: ['aligned-bam'], connections: ['iso-bam'], counts: [], color: '#3f658c'},
  'flep-seq': {related: ['aligned-bam'], connections: ['flep-bam'], counts: [], color: '#744c88'},
  ont: {related: ['aligned-bam'], connections: ['ont-bam'], counts: [], color: '#39787a'},
  'aligned-bam': {related: ['spliced', 'retained', 'specific', 'conflict', 'uninformative'], connections: ['evidence'], counts: []},
  spliced: {related: ['intron-counts'], connections: ['pooling'], counts: ['spliced'], color: '#3f658c'},
  retained: {related: ['intron-counts'], connections: ['pooling'], counts: ['retained'], color: '#427e59'},
  specific: {related: ['retained', 'intron-counts'], connections: ['pooling'], counts: ['retained', 'specific'], color: '#b94469'},
  conflict: {related: ['evidence-rules'], connections: [], counts: [], excluded: true, color: '#956020'},
  uninformative: {related: ['evidence-rules'], connections: [], counts: [], excluded: true, color: '#666b73'},
  'evidence-rules': {related: ['spliced', 'retained', 'specific', 'conflict', 'uninformative'], connections: [], counts: []},
  'sample-pooling': {related: ['intron-counts', 'ir-metrics'], connections: ['merge'], counts: []},
  'intron-counts': {related: ['sample-pooling', 'ir-metrics'], connections: ['merge'], counts: ['retained', 'spliced', 'specific', 'informative']},
  'ir-metrics': {related: ['intron-counts', 'high-confidence'], connections: ['criteria'], counts: ['retained', 'spliced', 'specific', 'informative']},
  'high-confidence': {related: ['ir-metrics', 'final-prior'], connections: ['criteria', 'prior'], counts: []},
  'final-prior': {related: ['high-confidence'], connections: ['prior'], counts: []},
}

// Tight rows in the original Intron A table, in the original SVG coordinate space.
export const countBounds: Record<Count, [number, number, number, number]> = {
  retained: [113.5, 269, 45, 11.5],
  spliced: [113.5, 280.8, 45, 11],
  specific: [113.5, 292, 45, 11],
  informative: [113.5, 304, 45, 12],
}

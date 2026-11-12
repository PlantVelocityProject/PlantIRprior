// Hotspots measured from the revised progress.pdf. Artwork groups live in assets/methods/overview.svg.
export type FigureModule = {
 id:string; title:string; description:string; bounds:number[]
}

export const figureViewport={"x":39,"y":23,"width":540,"height":379}

export const figureModules:FigureModule[] = [
 {
  "id": "iso-seq",
  "title": "Iso-seq",
  "description": "Full-length transcript reads preserve exon–intron structure for genome alignment.",
  "bounds": [
   50.3,
   45.2,
   86.1,
   86.7
  ]
 },
 {
  "id": "flep-seq",
  "title": "FLEP-seq",
  "description": "Long-read RNA evidence captures transcript structure and processing context.",
  "bounds": [
   143.9,
   45.2,
   86.2,
   86.7
  ]
 },
 {
  "id": "ont",
  "title": "ONT",
  "description": "Nanopore long reads provide transcript evidence with platform metadata.",
  "bounds": [
   237.1,
   45.2,
   86.1,
   86.7
  ]
 },
 {
  "id": "aligned-bam",
  "title": "Aligned BAM files",
  "description": "Genome-aligned reads anchor exon boundaries and splice junctions.",
  "bounds": [
   118.7,
   152,
   133.9,
   56.2
  ]
 },
 {
  "id": "spliced",
  "title": "Spliced evidence",
  "description": "Reads support intron removal through a junction between flanking exons.",
  "bounds": [
   351,
   45.9,
   216.3,
   54.5
  ]
 },
 {
  "id": "retained",
  "title": "Retained evidence",
  "description": "Reads cover the intron while supporting both flanking exon anchors.",
  "bounds": [
   350.9,
   107.6,
   217.1,
   53.6
  ]
 },
 {
  "id": "specific",
  "title": "Retention-specific evidence",
  "description": "Reads retain the target intron and splice every other overlapping intron.",
  "bounds": [
   351,
   168,
   217.1,
   53.7
  ]
 },
 {
  "id": "conflict",
  "title": "Conflicting evidence",
  "description": "Incompatible retained and spliced signals are excluded from IR counts.",
  "bounds": [
   350.6,
   227.8,
   104.6,
   57.4
  ]
 },
 {
  "id": "uninformative",
  "title": "Uninformative evidence",
  "description": "Insufficient read evidence for a retained or spliced assignment.",
  "bounds": [
   463,
   227.6,
   104.5,
   57.6
  ]
 },
 {
  "id": "evidence-rules",
  "title": "Read–intron evidence rules",
  "description": "Rules for classifying reads by exon anchors, junctions, and intron coverage.",
  "bounds": [
   350.2,
   291.6,
   217.1,
   41.1
  ]
 },
 {
  "id": "sample-pooling",
  "title": "Pooling samples",
  "description": "Combine matching samples before calculating intron-level metrics.",
  "bounds": [
   77.4,
   232,
   214.8,
   16.3
  ]
 },
 {
  "id": "intron-counts",
  "title": "Pooled intron counts",
  "description": "Sum retained, spliced, and specific evidence across samples.",
  "bounds": [
   96.3,
   251.7,
   66.7,
   65.9
  ]
 },
 {
  "id": "ir-metrics",
  "title": "IR metrics",
  "description": "Calculate IR ratio and specificity from pooled counts.",
  "bounds": [
   165.8,
   251.7,
   103.5,
   65.8
  ]
 },
 {
  "id": "high-confidence",
  "title": "Identify high-confidence IR",
  "description": "Apply confidence thresholds to select reliable intron-retention events.",
  "bounds": [
   84.7,
   335.7,
   203.2,
   62.8
  ]
 },
 {
  "id": "final-prior",
  "title": "High-confidence IR prior",
  "description": "Species-specific intron-retention events that pass all confidence thresholds.",
  "bounds": [
   364.5,
   347.2,
   191.2,
   49.9
  ]
 }
]

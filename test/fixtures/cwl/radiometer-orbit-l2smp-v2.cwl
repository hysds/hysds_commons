cwlVersion: v1.2
class: Workflow
id: radiometer-orbit-l2smp
label: Radiometer orbit to L2SMP
doc: L0B granules for one orbit through L1A, L1B, L1C and L2SMP, a browse image in parallel, and a QA decision before delivery.

$namespaces:
  hysds: https://github.com/hysds/hysds_commons/cwl/v1#
  s: https://schema.org/
s:version: 1.0.0

requirements:
  ScatterFeatureRequirement: {}
  MultipleInputFeatureRequirement: {}

hints:                                   # workflow level: least specific
  hysds:OnError: {mode: kill}
  hysds:Queue: {queue: radiometer-default}
  hysds:Retry: {max_retries: 1, retry_on: [offline, worker]}

inputs:
  orbit_id: string
  ancillary_version: {type: string, default: v7}
  reprocess_ancillary_version: {type: string, default: v7-fallback}

outputs:
  product:
    type: File?
    outputSource: [reprocess_l2smp/product, l2smp/product]
    pickValue: first_non_null
  browse:
    type: File
    outputSource: make_browse/browse_png
  receipt:
    type: string?
    outputSource: deliver/receipt

steps:
  find_l0b:
    run:
      class: Operation
      inputs:  {orbit_id: string}
      outputs: {l0b_files: "File[]"}
      hints: {hysds:JobType: {job_type: "job-radiometer-find-l0b:v1.0"}}
    in:  {orbit_id: orbit_id}
    out: [l0b_files]

  l1a:                                   # one step node; N child jobs
    run:
      class: Operation
      inputs:  {l0b_file: File, ancillary_version: string}
      outputs: {l1a_file: File}
      hints: {hysds:JobType: {job_type: "job-radiometer-l1a:v3.2"}}
    scatter: l0b_file
    in:
      l0b_file: find_l0b/l0b_files
      ancillary_version: ancillary_version
    out: [l1a_file]
    hints:
      hysds:Queue: {queue: radiometer-large}
      hysds:Retry: {max_retries: 2, retry_on: ["exit_code:137", offline]}
      hysds:Gather: {tolerated_failure_percentage: 5}
      ToolTimeLimit: {timelimit: 3600}

  l1b:
    run:
      class: Operation
      inputs:  {l1a_files: "File[]", orbit_id: string}
      outputs: {l1b_file: File}
      hints: {hysds:JobType: {job_type: "job-radiometer-l1b:v3.2"}}
    in:
      l1a_files:                         # drop failed items (canonical form)
        source: [l1a/l1a_file]
        linkMerge: merge_flattened
        pickValue: all_non_null
      orbit_id: orbit_id
    out: [l1b_file]

  l1c:
    run:
      class: Operation
      inputs:  {l1b_file: File}
      outputs: {l1c_file: File}
      hints: {hysds:JobType: {job_type: "job-radiometer-l1c:v3.2"}}
    in:  {l1b_file: l1b/l1b_file}
    out: [l1c_file]

  make_browse:                           # runs in parallel with l2smp
    run:
      class: Operation
      inputs:  {l1c_file: File}
      outputs: {browse_png: File}
      hints: {hysds:JobType: {job_type: "job-radiometer-browse:v1.0"}}
    in:  {l1c_file: l1c/l1c_file}
    out: [browse_png]

  l2smp:
    run:
      class: Operation
      inputs:  {l1c_file: File, ancillary_version: string}
      outputs: {product: File}
      hints: {hysds:JobType: {job_type: "job-radiometer-l2smp:v2.0"}}
    in:  {l1c_file: l1c/l1c_file, ancillary_version: ancillary_version}
    out: [product]

  qa_check:                              # decision job: writes booleans
    run:
      class: Operation
      inputs:  {product: File}
      outputs: {deliver: boolean, reprocess: boolean, reject: boolean, qa_score: float}
      hints: {hysds:JobType: {job_type: "job-radiometer-l2smp-qa:v1.0"}}
    in:  {product: l2smp/product}
    out: [deliver, reprocess, reject, qa_score]

  reprocess_l2smp:
    when: $(inputs.go)
    run:
      class: Operation
      inputs:  {l1c_file: File, ancillary_version: string}   # job params only
      outputs: {product: File}
      hints: {hysds:JobType: {job_type: "job-radiometer-l2smp:v2.0"}}
    in:
      go: qa_check/reprocess             # used by `when` only, not passed to the job
      l1c_file: l1c/l1c_file
      ancillary_version: reprocess_ancillary_version
    out: [product]

  reject:
    when: $(inputs.go)
    run:
      class: Operation
      inputs: {}
      outputs: {}
      hints: {hysds:Halt: {status: failed, reason: "L2SMP failed QA"}}
    in:  {go: qa_check/reject}
    out: []

  deliver:                               # join: waits for browse and (maybe) reprocess
    when: $(inputs.go)
    run:
      class: Operation
      inputs:  {product: File, browse: File, orbit_id: string}
      outputs: {receipt: string}
      hints: {hysds:JobType: {job_type: "job-radiometer-deliver:v1.1"}}
    in:
      go: qa_check/deliver
      product:
        source: [reprocess_l2smp/product, l2smp/product]
        pickValue: first_non_null
      browse: make_browse/browse_png
      orbit_id: orbit_id
    out: [receipt]

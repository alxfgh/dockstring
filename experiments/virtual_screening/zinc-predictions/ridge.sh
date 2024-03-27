#!/usr/bin/env bash
target_arr=( KIT PARP1 PGR )
method_name="ridge"

if [[ -z "pred_csv" ]] ; then
    echo "pred_csv variable is not defined! Needs to be defined."
    exit 1
fi

curr_expt_idx=0
for target in "${target_arr[@]}" ; do

    # Result dir for this target
    res_dir="./results/virtual-screening/${method_name}/${target}"
    mkdir -p "${res_dir}"

    # Run multiple trials
    for trial in {0..0}; do
        output_dir="${res_dir}/predictions-trial-${trial}"
        output_path="${output_dir}/$(basename $pred_csv)"

        if [[ -f "$output_path" ]]; then
            echo "Results for ${target} trial ${trial} exists. Skipping"

        elif [[ -z "$expt_idx" || "$expt_idx" = "$curr_expt_idx" ]] ; then

            echo "Running ${target} trial ${trial}..."

            mkdir -p "$output_dir"
            PYTHONPATH="$(pwd)/src:$PYTHONPATH" python src/virtual_screening/${method_name}.py \
                --dataset="$pred_csv" \
                --pred_save_path="$output_path" \
                --load_model_dir="${res_dir}/model-${trial}"  \

        fi

        # Increment experiment index after every trial
        curr_expt_idx=$(( curr_expt_idx + 1 ))

    done

done

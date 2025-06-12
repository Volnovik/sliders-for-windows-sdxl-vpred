# Sliders train script by @bdsqlsz

#SD version(Lora、Sdxl_lora)
$sd_version = "sdxl_lora"

#train_mode(text,image)
$train_mode = "image"

#output name
$name = "generic1000"

# Train data config | 设置训练配置路径
$config_file = "./trainscripts/imagesliders/data/config-xl_nvp.yaml" # config path | 配置路径

# main body attributes| 主体属性
$attributes = ""

#LoRA rank and alpha
$rank = 8
$alpha = 2

#image model
$folder_main = "./datasets/generic/"
$folders = "bad, good"
$scales = "-2, 2"

# ============= DO NOT MODIFY CONTENTS BELOW | 请勿修改下方内容 =====================
# Activate python venv
Set-Location $PSScriptRoot
.\venv\Scripts\activate

$Env:HF_HOME = "huggingface"
$ext_args = [System.Collections.ArrayList]::new()

if ($train_mode -ieq "text") {
  $laungh_script = "textsliders/train_lora"
  if ($sd_version -ilike "sdxl*") {
    $laungh_script = $laungh_script + "_xl"
  }
}
else {
  $laungh_script = "imagesliders/train_lora-scale"
  [void]$ext_args.Add("--folder_main=$folder_main")
  [void]$ext_args.Add("--folders=$folders")
  [void]$ext_args.Add("--scales=$scales")
  if ($sd_version -ilike "sdxl*") {
    $laungh_script = $laungh_script + "-xl"
  }
}



# run train
python -m accelerate.commands.launch --num_cpu_threads_per_process=8 "./trainscripts/$laungh_script.py" `
  --config_file=$config_file `
  --attributes=$attributes `
  --name=$name `
  --rank=$rank `
  --alpha=$alpha $ext_args

Write-Output "Train finished"
Read-Host | Out-Null ;
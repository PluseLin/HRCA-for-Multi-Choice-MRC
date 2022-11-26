# An Implement of HRCA and HRCA+

## 1.Introduction

* This is an implement of HRCA and HRCA+,which are proposed in the paper:HRCA+: Advanced Multiple-choice Machine Reading Comprehension Method.I also add pdf in this repo.This project is for graduatation design of Tongji University.

* My Inplement is based on another repo: `https://github.com/pfZhu/duma_code` ,for the reason that both DUMA and HRCA use mulit-head Attention.

## 2. Usage

* git clone my repo
* run in Linux:

  ```Linux
  pip install -r requirements.txt
  ```

* run in Linux:

  ```Linux
  sh run_dream.sh
  ```

## 3.Details

* The number of layers of HRCA and HRCA+ are 4.
* Currently the code is using HRCA+,if you want to use HRCA,just modify line 70 in 'run_multiple_choice.py'
* Some of requirement in requirement is not used,however I fail to check them out,so if you meet failure in pip installing,perhaps you could try to DELETE it in 'requirements.txt'.
* I do not use fp16 because I failed to install nvidia apex.If you want to use fp16,please refer to the REAMDE of `https://github.com/pfZhu/duma_code`.

## 4.Results of my implement

* It's pity that my results are not as good as the results of the paper,which I think is because the implements of HRCA and HRCA+ are not perfect and the parameters could be adjusted better.

* This is my results on a single V100 GPU:

    Model | acc of DREAM | learning rate | epoches| batch_size
    --- | --- | --- | ---| ---
    Albert_base+HRCA | 66.7 | 2e-5 | 3| 2
    Albert_base+HRCA+ | 67.8| 1e-5| 3| 2

## 5.Contact me

* If you would like to contact me,please send email to 1951444@tongji.edu.cn. Both Chinese and English are OK.
* Please STAR this repo if you think it is helpful.Thanks.

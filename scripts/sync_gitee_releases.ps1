param(
    [string]$OnlyTag
)
$ErrorActionPreference = 'Stop'

$owner = 'orange-blade'
$repo = 'DSTCamp'
$githubRepo = 'chengzhirenchaoshuai/DSTCamp'
$keepCount = 3
# 令牌放在项目外的密钥目录（不进 Git），可用环境变量 DSTCAMP_KEY_DIR 改位置
$keyDir = if ($env:DSTCAMP_KEY_DIR) { $env:DSTCAMP_KEY_DIR } else { 'F:\AI\mywork\ProjectKey\DSTCamp' }
$tokenPath = Join-Path $keyDir 'gitee_token.txt'

if (-not (Test-Path -LiteralPath $tokenPath -PathType Leaf)) {
    throw "未找到 Gitee 令牌文件：$tokenPath"
}

$token = (Get-Content -Raw -Encoding UTF8 -LiteralPath $tokenPath).Trim()
if ([string]::IsNullOrWhiteSpace($token)) {
    throw 'Gitee 令牌文件为空。'
}

$headers = @{ 'User-Agent' = 'DSTCamp-release-sync' }

# Gitee 发行版说明不支持表情符号，同步前去掉表情及其后的一个空格，
# "## ✨ 1.5.1 亮点" 变为 "## 1.5.1 亮点"。
# 注意：Gitee 还会审核说明文字，含"代理"等字样时整段被替换成"内容可能含有违规信息"
# （1.5.1 实测），这类措辞只能在 Gitee 上手动改写，同步后要回读确认。
function Remove-Emoji([string]$Text) {
    return $Text -replace '(?:[\uD800-\uDBFF][\uDC00-\uDFFF]|[☀-➿⬀-⯿])[️‍]*[ \t]?', ''
}
$allGithubReleases = @((Invoke-WebRequest -Uri "https://api.github.com/repos/$githubRepo/releases?per_page=100" -Headers $headers).Content | ConvertFrom-Json)
# 版本发行版（vX.Y.Z）只保留最新 $keepCount 个；工具发行版（tools-N，按需下载的工具，见 tool_store.py）保留编号最大的一个。
# 先按标签格式筛选再转 [version]，否则 tools-N 转换失败会中断整个脚本
$retainedReleases = @($allGithubReleases |
    Where-Object { $_.tag_name -match '^v\d+\.\d+\.\d+$' -and [version]($_.tag_name.TrimStart('v')) -ge [version]'1.0.0' } |
    Sort-Object { [version]$_.tag_name.TrimStart('v') } -Descending |
    Select-Object -First $keepCount)
$retainedReleases += @($allGithubReleases |
    Where-Object { $_.tag_name -match '^tools-\d+$' } |
    Sort-Object { [int]($_.tag_name -replace '^tools-', '') } -Descending |
    Select-Object -First 1)
$githubReleases = @($retainedReleases | Where-Object {
    [string]::IsNullOrWhiteSpace($OnlyTag) -or $_.tag_name -eq $OnlyTag
})
$giteeReleases = Invoke-RestMethod -Uri "https://gitee.com/api/v5/repos/$owner/$repo/releases?per_page=100&access_token=$token"
$keepTags = @($retainedReleases | ForEach-Object tag_name)
foreach ($oldRelease in @($giteeReleases)) {
    if ($oldRelease.tag_name -match '^(v\d+\.\d+\.\d+|tools-\d+)$' -and $keepTags -notcontains $oldRelease.tag_name) {
        Invoke-RestMethod -Method Delete -Uri "https://gitee.com/api/v5/repos/$owner/$repo/releases/$($oldRelease.id)?access_token=$token"
        Write-Output "删除旧发行版：$($oldRelease.tag_name)"
    }
}
$giteeReleases = @($giteeReleases | Where-Object { $keepTags -contains $_.tag_name })
$existingTags = @{}
foreach ($release in $giteeReleases) {
    $existingTags[$release.tag_name] = $release
}

$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('dstcamp-release-sync-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempRoot | Out-Null
$created = 0
$uploaded = 0
$skipped = 0

try {
    foreach ($release in ($githubReleases | Sort-Object published_at)) {
        if ($existingTags.ContainsKey($release.tag_name)) {
            $giteeRelease = $existingTags[$release.tag_name]
            Write-Output "检查已存在发行版：$($release.tag_name)"
            $skipped++
        }
        else {
            $releaseBody = @{
                access_token = $token
                tag_name = $release.tag_name
                name = $release.name
                body = Remove-Emoji $release.body
                prerelease = [bool]$release.prerelease
                target_commitish = $release.target_commitish
            } | ConvertTo-Json -Depth 3

            $giteeRelease = Invoke-RestMethod `
                -Method Post `
                -Uri "https://gitee.com/api/v5/repos/$owner/$repo/releases" `
                -ContentType 'application/json; charset=utf-8' `
                -Body $releaseBody
            $created++
            Write-Output "创建发行版：$($release.tag_name)"
        }

        $existingAssets = @($giteeRelease.assets | ForEach-Object name)

        foreach ($asset in $release.assets) {
            if ($existingAssets -contains $asset.name) {
                Write-Output "  跳过已有附件：$($asset.name)"
                continue
            }
            $localFile = Join-Path $tempRoot $asset.name
            Invoke-WebRequest -Uri $asset.browser_download_url -Headers $headers -OutFile $localFile
            $uploadUri = "https://gitee.com/api/v5/repos/$owner/$repo/releases/$($giteeRelease.id)/attach_files"
            $uploadResult = & curl.exe -sS --fail-with-body --connect-timeout 15 --max-time 180 --retry 2 --retry-delay 3 -X POST -F "access_token=$token" -F "file=@$localFile" $uploadUri 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Warning ("上传附件失败：{0} / {1}; Gitee 返回：{2}" -f $release.tag_name, $asset.name, ($uploadResult -join ' '))
                continue
            }
            $uploaded++
            Remove-Item -LiteralPath $localFile -Force
            Write-Output "  上传附件：$($asset.name)"
        }
    }
}
finally {
    if (Test-Path -LiteralPath $tempRoot) {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force
    }
}

Write-Output "同步完成：创建 $created 个发行版，上传 $uploaded 个附件，跳过 $skipped 个发行版。"

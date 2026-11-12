"""Package a built Windows executable with public docs and dependency licenses."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import zipfile


def dependency_licenses(lockfile):
    licenses = {}
    for line in lockfile.read_text(encoding='utf-8').splitlines():
        if '==' not in line or line.lstrip().startswith('#'):
            continue
        distribution = importlib.metadata.distribution(line.split('==')[0].strip())
        for entry in distribution.files or ():
            if not any('dist-info' in part for part in entry.parts):
                continue
            if not any(part.lower() in {'license', 'licenses', 'copying', 'notice'}
                       or part.lower().startswith(('license.', 'licence.', 'copying.', 'notice.'))
                       for part in entry.parts):
                continue
            path = Path(distribution.locate_file(entry))
            if path.is_file():
                licenses[entry.as_posix()] = path.read_bytes()
    python_license = Path(sys.base_prefix)/'LICENSE.txt'
    if python_license.is_file():
        licenses['Python-LICENSE.txt'] = python_license.read_bytes()
    return licenses


def package(root, binary, output, *, version):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?', version):
        raise ValueError('Version must be a release identifier')
    if not binary.is_file() or binary.suffix.lower() != '.exe':
        raise ValueError('A built Windows executable is required')
    with binary.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('Executable does not have a Windows PE header')
    docs = {name: (root/name).read_bytes() for name in
            ('README.md', 'PRIVACY.md', 'CHANGELOG.md', 'FEATURE_STATUS.md')}
    for name in ('LOCAL_SETUP.md', 'docs/VALIDATION-2026-11-12.md', 'docs/RELEASE_NOTES.md'):
        if (root/name).is_file():
            docs[name] = (root/name).read_bytes()
    licenses = dependency_licenses(root/'requirements-local-lock.txt')
    if not licenses:
        raise ValueError('Dependency license texts were not found')
    readme = f'''原神好友状态监测器 v{version}

Windows 10/11 x64 便携包。解压到可写目录后运行 GenshinFriendMonitor.exe。
无需安装 Python，请勿在压缩包内直接运行。直接运行会由 Windows 请求
管理员授权；取消授权则停止启动，不修改系统安全策略。

游戏须为完整的 16:9 客户区，不含标题栏，整个客户区位于同一屏幕内、
无遮挡且不最小化。默认普通屏幕采集；遮挡采集仍为实验功能。
启动后不会自动扫描。
先查找游戏窗口并识别当前账号，再手动开始。
首次可以点击“初始化档案”完整识别 UID，也可先快速扫描后补录。
UID 未齐只提示可能影响统计，不强制识别。
频率留空单次，0 连续，正数为分钟间隔；只有填写频率才持续累计时长。

当前包不含任何账号、好友资料、个人配置、日志或截图。
首次启动会在程序旁生成 config.json、data 和 logs，均属于个人数据。
升级已有安装时，请先停止扫描并关闭程序，备份 data 和 config.json，
再替换程序文件；保留自己的 data 和 config.json 可继续使用原有资料。
请勿把这些运行数据上传到公开仓库。详细范围见 PRIVACY.md。

状态变动记录只显示上线、下线、新增、消失/可能被删。
首次已在线不会记为刚上线，但按扫描频率正常统计在线时段。
减少的好友可勾选“显示历史档案”查看资料；删除判断需要完整扫描。

依赖许可见 THIRD_PARTY_LICENSES，功能限制见 FEATURE_STATUS.md，
本次版本说明见 docs/RELEASE_NOTES.md。
'''
    output.mkdir(parents=True, exist_ok=True)
    archive = output/f'GenshinFriendMonitor-windows-x64-v{version}.zip'
    prefix = f'GenshinFriendMonitor-v{version}/'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.write(binary, prefix+'GenshinFriendMonitor.exe')
        bundle.writestr(prefix+'使用说明.txt', readme.encode('utf-8-sig'))
        for name, data in sorted(docs.items()):
            bundle.writestr(prefix+name, data)
        for name, data in sorted(licenses.items()):
            bundle.writestr(prefix+'THIRD_PARTY_LICENSES/'+name, data)
    checksum = hashlib.sha256()
    with archive.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            checksum.update(chunk)
    digest = checksum.hexdigest()
    archive.with_suffix('.zip.sha256').write_text(digest+'  '+archive.name+'\n', encoding='ascii')
    return {'archive': archive.name, 'bytes': archive.stat().st_size,
            'dependency_license_files': len(licenses), 'sha256': digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    print(json.dumps(package(args.root, args.binary, args.output, version=args.version), ensure_ascii=False))


if __name__ == '__main__':
    main()

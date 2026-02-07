#!/usr/bin/env python3
"""
Generalized script to vendor lms_interface into any target project.

This script can be run from LMSInterface to vendor itself into other projects.

Usage:
    python scripts/vendor_into_project.py /path/to/target/project --target-package TargetPackage --vendor-name canvas
    python scripts/vendor_into_project.py /path/to/target/project --top-level
    python scripts/vendor_into_project.py /path/to/target/project --dry-run
"""

import argparse
import re
import shutil
import sys
from pathlib import Path
from datetime import datetime


# Files to copy from lms_interface
FILES_TO_COPY = ['__init__.py', 'canvas_interface.py', 'classes.py']


def get_version(source_repo: Path) -> str:
    """Extract version from pyproject.toml"""
    pyproject = source_repo / "pyproject.toml"
    if not pyproject.exists():
        return "unknown"

    content = pyproject.read_text()
    match = re.search(r'version\s*=\s*"([^"]+)"', content)
    return match.group(1) if match else "unknown"


def copy_source_files(source_dir: Path, target_dir: Path, files_to_copy: list, dry_run: bool = False):
    """Copy source files to target directory"""
    print(f"\nCopying files from {source_dir} to {target_dir}")

    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)

    for filename in files_to_copy:
        source = source_dir / filename
        dest = target_dir / filename

        if not source.exists():
            print(f"  Warning: {source} not found, skipping")
            continue

        if dry_run:
            print(f"  [DRY RUN] Would copy: {filename}")
        else:
            shutil.copy2(source, dest)
            print(f"  Copied: {filename}")

    return True


def add_version_header(init_file: Path, version: str, source_name: str, target_desc: str, dry_run: bool = False):
    """Add version tracking header to __init__.py"""
    version_header = f'''"""
{target_desc}

Vendored from {source_name} v{version} ({datetime.now().strftime('%Y-%m-%d')})
"""

__version__ = "{version}"
__vendored_from__ = "{source_name}"
__vendored_date__ = "{datetime.now().strftime('%Y-%m-%d')}"

'''

    print(f"\nUpdating {init_file.name} with version info")

    if dry_run:
        print(f"  [DRY RUN] Would add version header for v{version}")
        return True

    # Read existing content (if any)
    existing_content = ""
    if init_file.exists():
        existing_content = init_file.read_text()
        # Remove old version header if it exists
        existing_content = re.sub(
            r'^""".*?""".*?__vendored_date__.*?\n\n',
            '',
            existing_content,
            flags=re.DOTALL | re.MULTILINE
        )

    # Write new version header + existing content
    init_file.write_text(version_header + existing_content.lstrip())
    print(f"  Updated with version {version}")

    return True


def update_imports_in_file(file_path: Path, target_package: str, vendor_name: str, dry_run: bool = False):
    """Update all lms_interface imports in a single Python file"""
    if not file_path.suffix == '.py':
        return False

    try:
        content = file_path.read_text()
    except (UnicodeDecodeError, PermissionError):
        return False

    original_content = content

    # Simple pattern: replace any "lms_interface" with "{target_package}.{vendor_name}"
    # This handles: from lms_interface.X import Y, import lms_interface.X, etc.
    new_content = content.replace('lms_interface', f'{target_package}.{vendor_name}')

    if new_content != original_content:
        if dry_run:
            # Show what would change
            print(f"  [DRY RUN] Would update {file_path.relative_to(file_path.parents[2])}:")
            lines_old = original_content.split('\n')
            lines_new = new_content.split('\n')
            for i, (old, new) in enumerate(zip(lines_old, lines_new)):
                if old != new and 'lms_interface' in old:
                    print(f"    Line {i+1}: {old.strip()} -> {new.strip()}")
        else:
            file_path.write_text(new_content)
            print(f"  Updated {file_path.relative_to(file_path.parents[2])}")
        return True

    return False


def walk_and_update_imports(target_package_dir: Path, target_package: str, vendor_name: str, dry_run: bool = False):
    """Walk through all Python files and update lms_interface imports"""
    print(f"\nScanning for lms_interface imports in {target_package_dir.name}/")

    updated_files = []
    vendor_dir = target_package_dir / vendor_name

    for py_file in target_package_dir.rglob('*.py'):
        # Skip the vendored directory itself
        if vendor_dir.exists() and (vendor_dir in py_file.parents or py_file.parent == vendor_dir):
            continue

        if update_imports_in_file(py_file, target_package, vendor_name, dry_run):
            updated_files.append(py_file)

    if updated_files:
        print(f"\nUpdated imports in {len(updated_files)} file(s)")
    else:
        print("\nNo files needed import updates")

    return True


def update_pyproject_dependencies(target_root: Path, source_repo: Path, dry_run: bool = False):
    """Update pyproject.toml to remove lms-interface dependency and add vendored deps"""
    pyproject = target_root / "pyproject.toml"

    if not pyproject.exists():
        print(f"\nWarning: {pyproject} not found, skipping dependency update")
        return True

    print(f"\nUpdating pyproject.toml")

    content = pyproject.read_text()

    # Get dependencies from source
    source_pyproject = source_repo / "pyproject.toml"
    source_deps = []
    if source_pyproject.exists():
        source_content = source_pyproject.read_text()
        in_deps = False
        for line in source_content.split('\n'):
            if 'dependencies = [' in line:
                in_deps = True
                continue
            if in_deps:
                if line.strip().startswith(']'):
                    break
                if line.strip() and not line.strip().startswith('#'):
                    dep = line.strip().strip(',').strip('"').strip("'")
                    if dep:
                        source_deps.append(dep)

    print(f"  Found lms-interface dependencies: {source_deps}")

    changes = []

    # Remove lms-interface dependency
    for quote in ['"', "'"]:
        pattern = f'{quote}lms-interface{quote}'
        if pattern in content:
            changes.append("Remove lms-interface from dependencies")
            if not dry_run:
                content = re.sub(rf'\s*{quote}lms-interface{quote}[,\s]*\n', '', content)

    # Remove uv.sources if it references lms-interface
    if '[tool.uv.sources]' in content and 'lms-interface' in content:
        changes.append("Remove [tool.uv.sources] reference")
        if not dry_run:
            content = re.sub(
                r'\[tool\.uv\.sources\]\s*\nlms-interface\s*=\s*\{[^}]+\}\s*\n',
                '',
                content
            )

    if dry_run:
        print(f"  [DRY RUN] Would make changes:")
        for change in changes:
            print(f"    - {change}")
        print(f"  Note: Ensure these dependencies are present:")
        for dep in source_deps:
            print(f"    {dep}")
    else:
        if changes:
            pyproject.write_text(content)
            print(f"  Made {len(changes)} change(s)")

        print(f"\n  ACTION REQUIRED: Verify these dependencies are present:")
        for dep in source_deps:
            print(f"    {dep}")

    return True


def update_hatch_packages(target_root: Path, vendor_name: str, dry_run: bool = False):
    """Ensure vendored package is included in Hatch packages list"""
    pyproject = target_root / "pyproject.toml"
    if not pyproject.exists():
        print(f"\nWarning: {pyproject} not found, skipping package inclusion update")
        return True

    content = pyproject.read_text()
    section_match = re.search(
        r'(\[tool\.hatch\.build\.targets\.wheel\][\s\S]*?)(\n\[|$)',
        content
    )
    if not section_match:
        print("\nWarning: [tool.hatch.build.targets.wheel] not found; ensure packages include vendored module")
        return True

    section = section_match.group(1)
    packages_match = re.search(r'packages\s*=\s*\[(.*?)\]', section, re.DOTALL)
    if not packages_match:
        print("\nWarning: packages list not found in hatch wheel target; update manually if needed")
        return True

    raw_list = packages_match.group(1)
    existing = []
    for item in raw_list.split(','):
        item = item.strip().strip('"').strip("'")
        if item:
            existing.append(item)

    if vendor_name in existing:
        print(f"\nHatch packages already include {vendor_name}")
        return True

    updated = existing + [vendor_name]
    updated_list = ", ".join([f"\"{item}\"" for item in updated])
    new_section = re.sub(
        r'packages\s*=\s*\[(.*?)\]',
        f'packages = [{updated_list}]',
        section,
        flags=re.DOTALL
    )

    if dry_run:
        print(f"\n[DRY RUN] Would add {vendor_name} to hatch packages list")
        return True

    content = content.replace(section, new_section)
    pyproject.write_text(content)
    print(f"\nAdded {vendor_name} to hatch packages list")
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Vendor lms_interface into a target project"
    )
    parser.add_argument(
        "target_project",
        type=Path,
        help="Path to target project root"
    )
    parser.add_argument(
        "--target-package",
        help="Name of target package (e.g., 'QuizGenerator'). If not specified, will try to infer from pyproject.toml"
    )
    parser.add_argument(
        "--vendor-name",
        default="lms_interface",
        help="Name for vendored module (default: lms_interface)"
    )
    parser.add_argument(
        "--top-level",
        action="store_true",
        help="Vendor as a top-level package (avoids import rewrites)"
    )
    parser.add_argument(
        "--no-rewrite",
        action="store_true",
        help="Skip import rewrites even if not top-level"
    )
    parser.add_argument(
        "--source-repo",
        type=Path,
        help="Path to LMSInterface repository (default: script parent dir)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes"
    )

    args = parser.parse_args()

    # Determine source repository
    script_dir = Path(__file__).parent
    source_repo = args.source_repo or script_dir.parent

    # Determine target package name
    if args.target_package:
        target_package_name = args.target_package
    else:
        # Try to infer from target project
        pyproject = args.target_project / "pyproject.toml"
        if pyproject.exists():
            content = pyproject.read_text()
            match = re.search(r'name\s*=\s*"([^"]+)"', content)
            if match:
                target_package_name = match.group(1)
            else:
                print("Error: Could not determine target package name")
                print("Please specify --target-package")
                return 1
        else:
            print("Error: Could not find pyproject.toml in target")
            print("Please specify --target-package")
            return 1

    print("=" * 70)
    print("LMSInterface Vendoring Script")
    print("=" * 70)

    if args.dry_run:
        print("DRY RUN MODE - No changes will be made")

    print(f"\nConfiguration:")
    print(f"  Source repo:      {source_repo}")
    print(f"  Target project:   {args.target_project}")
    print(f"  Target package:   {target_package_name}")
    print(f"  Vendor name:      {args.vendor_name}")
    print(f"  Top-level:        {args.top_level}")

    # Verify paths exist
    source_package_dir = source_repo / 'lms_interface'
    if not source_package_dir.exists():
        print(f"\nError: lms_interface not found at {source_package_dir}")
        return 1

    target_package_dir = args.target_project / target_package_name
    if not args.top_level:
        if not target_package_dir.exists() and not args.dry_run:
            print(f"\nError: Target package directory not found at {target_package_dir}")
            return 1

    # Get version
    version = get_version(source_repo)
    print(f"\nVendoring lms_interface v{version}")

    # Execute vendoring
    target_vendor_dir = (args.target_project / args.vendor_name) if args.top_level else (target_package_dir / args.vendor_name)

    success = True
    success &= copy_source_files(
        source_package_dir,
        target_vendor_dir,
        FILES_TO_COPY,
        args.dry_run
    )

    success &= add_version_header(
        target_vendor_dir / "__init__.py",
        version,
        "LMSInterface",
        f"LMS integration for {target_package_name}",
        args.dry_run
    )

    if args.top_level or args.no_rewrite:
        print("\nSkipping import rewrites")
    else:
        success &= walk_and_update_imports(
            target_package_dir,
            target_package_name,
            args.vendor_name,
            args.dry_run
        )

    success &= update_pyproject_dependencies(
        args.target_project,
        source_repo,
        args.dry_run
    )
    if args.top_level:
        success &= update_hatch_packages(
            args.target_project,
            args.vendor_name,
            args.dry_run
        )

    print("\n" + "=" * 70)
    if args.dry_run:
        print("Dry run complete! Run without --dry-run to apply changes")
    elif success:
        print("Vendoring complete!")
        print("\nNext steps:")
        print("  1. Review changes: git diff")
        print("  2. Verify dependencies in pyproject.toml")
        print("  3. Test imports work correctly")
        if args.top_level:
            print(f"  4. Commit vendored code: git add {args.vendor_name}/")
        else:
            print(f"  4. Commit vendored code: git add {target_package_name}/{args.vendor_name}/")
    else:
        print("Completed with warnings - please review output above")
        return 1

    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())

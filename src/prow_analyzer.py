import json
import logging
import os
import re
from collections import deque
from pathlib import Path
from src.constants import BUILD_LOG_TAIL, MAINTENANCE_ISSUE
from src.log_summarizer import search_prow_errors
from src.utils import categorize_prow_failure
from src.xmlparser import (
    summarize_orion_xml, 
    summarize_junit_operator_xml
)

logger = logging.getLogger(__name__)


def filter_noise_patterns(text):
    """
    Filters out specific noise patterns from text while preserving actual error content.
    
    :param text: text to filter
    :return: filtered text
    """
    if not text:
        return text
    
    # Specific noise lines to remove completely (exact matches or patterns)
    noise_lines = [
        # Pod failure messages
        r'.*pod.*failed: could not watch pod:.*failed after.*ContainerFailed one or more containers exited',
        # Entrypoint error messages
        r'\{.*"component":"entrypoint".*"error":"wrapped process failed: exit status 1".*"Error executing test process".*\}',
        # Container exit messages
        r'.*Container test exited with code 1, reason Error',
        # Wrapped command execution errors
        r'error: failed to execute wrapped command: exit status 1',
        # Logs for container test in pod messages (header noise)
        r'Logs for container test in pod .*:',
        # Link to registry info site
        r'Link to.*',
        # Separator lines
        r'^---+$',
        # Empty lines or whitespace-only lines
        r'^\s*$',
    ]
    
    # Split the text into lines and filter out noise lines
    lines = text.split('\n')
    filtered_lines = []
    
    for line in lines:
        line_stripped = line.strip()
        if not line_stripped:  # Keep empty lines
            filtered_lines.append(line)
            continue
            
        # Check if this line matches any noise pattern
        is_noise = False
        for pattern in noise_lines:
            if re.search(pattern, line_stripped, re.DOTALL):
                is_noise = True
                break
        
        if not is_noise:
            # Clean ANSI color codes
            cleaned_line = re.sub(r'\x1b\[[0-9;]*m', '', line)
            cleaned_line = re.sub(r'\[[0-9;]*m', '', cleaned_line)
            # Clean unicode replacement characters
            cleaned_line = re.sub(r'[^\x00-\x7F]+', '', cleaned_line)
            # Clean timestamp patterns
            timestamp_pattern = r'^(?:\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:Z)?|\s+\d{2}:\d{2}:\d{2})|\d{2}-\d{2}-\d{4}T\d{2}:\d{2}:\d{2})\s*'
            cleaned_line = re.sub(timestamp_pattern, '', cleaned_line)
            # Clean any remaining whitespace at the beginning
            cleaned_line = cleaned_line.lstrip()
            
            # Only add the line if it still has content after cleaning
            if cleaned_line:
                filtered_lines.append(cleaned_line)
    
    # Reconstruct the text with filtered lines
    return '\n'.join(filtered_lines).strip()


def get_cluster_operator_errors(directory_path):
    """
    Extracts errors from the clusteroperators.json.

    :param directory_path: directory path for the artifacts
    :return: list of errors
    """
    try:
        with open(
            f"{directory_path}/clusteroperators.json", "r", encoding="utf-8"
        ) as f:
            cluster_operators_data = json.load(f)
        err_conditions = []
        for each_item in cluster_operators_data["items"]:
            each_dict = {"Name": each_item["metadata"]["name"]}
            for condition in each_item["status"]["conditions"]:
                if (
                    condition["type"] == "Degraded" and condition["status"] == "True"
                ) or (
                    condition["type"] == "Available" and condition["status"] == "False"
                ):
                    each_dict["Status"] = condition["status"]
                    each_dict["Reason"] = condition["reason"]
                    each_dict["Message"] = condition["message"]
                    err_conditions.append(json.dumps(each_dict))
        return err_conditions
    except Exception as e:
        logger.error("Failed to fetch log file: %s", e)
        return []


def scan_orion_xmls(directory_path):
    """
    Extracts errors from orion xmls.

    :param directory_path: directory path for the artifacts
    :return: list of errors
    """
    base_dir = Path(f"{directory_path}/orion")
    xml_files = base_dir.glob("*.xml")
    for xml_file in xml_files:
        xml_content = summarize_orion_xml(xml_file)
        if xml_content != "":
            return [xml_content]
    return []


def analyze_prow_artifacts(directory_path, job_name):
    """
    Analyzes prow artifacts and extracts errors.

    :param directory_path: directory path for the artifacts
    :param job_name: job name to base line with
    :return: tuple of (list of errors, categorization_message, requires_llm, is_install_issue)
    """
    step_summary = ""
    categorization_message = ""
    pattern = re.compile(r"Logs for container test in pod .*")
    timestamp_strip = re.compile(r"^\x1b\[[0-9;]*m\w*\x1b\[0m\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]\s*")
    build_file_path = os.path.join(directory_path, "build-log.txt")
    if not os.path.isfile(build_file_path):
        return [
            "Prow maintanence issues, couldn't even find the build-log.txt file"
        ], MAINTENANCE_ISSUE, False, True
    with open(build_file_path, "r", errors="replace", encoding="utf-8") as f:
        matched_line = next(
            (
                timestamp_strip.sub("", line).strip()
                for line in f
                if pattern.search(timestamp_strip.sub("", line))
            ),
            None
        )
        if matched_line is None:
            matched_line = (
                "Couldn't identify the failure step, likely a maintanence issue"
            )
            return [matched_line], MAINTENANCE_ISSUE, False, True
        
        # Apply noise filtering to the matched line
        matched_line = filter_noise_patterns(matched_line)
    junit_operator_file_path = os.path.join(directory_path, "junit_operator.xml")
    if os.path.isfile(junit_operator_file_path):
        step_phase, step_name, step_summary = summarize_junit_operator_xml(junit_operator_file_path)
        categorization_message = categorize_prow_failure(step_name, step_phase)
    cluster_operators_file_path = os.path.join(directory_path, "clusteroperators.json")
    if not os.path.isfile(cluster_operators_file_path):
        with open(build_file_path, "r", errors="replace", encoding="utf-8") as f:
            build_log_content = list(deque(f, maxlen=BUILD_LOG_TAIL))
        return [
            "\n Somehow couldn't find clusteroperators.json file",
            matched_line + "\n",
            [step_summary] + "\n".join(build_log_content),
        ], categorization_message, False, True
    cluster_operator_errors = get_cluster_operator_errors(directory_path)
    if len(cluster_operator_errors) == 0:
        orion_errors = scan_orion_xmls(directory_path)
        if len(orion_errors) == 0:
            errors = [matched_line] + [step_summary] + search_prow_errors(directory_path, job_name)
            # Apply noise filtering to all errors
            filtered_errors = []
            for error in errors:
                filtered_error = filter_noise_patterns(error)
                if filtered_error:
                    filtered_errors.append(filtered_error)
            return filtered_errors, categorization_message, True, False
        # Filter matched_line but preserve Orion errors as-is
        filtered_matched_line = filter_noise_patterns(matched_line + "\n")
        errors = [filtered_matched_line] + orion_errors if filtered_matched_line else orion_errors
        return errors, categorization_message, False, False
    # Filter matched_line but preserve cluster operator errors as-is
    filtered_matched_line = filter_noise_patterns(matched_line + "\n")
    errors = [filtered_matched_line] + cluster_operator_errors if filtered_matched_line else cluster_operator_errors
    return errors, categorization_message, False, False

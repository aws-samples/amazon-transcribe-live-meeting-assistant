/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */

/**
 * @author Solution Builders
 */

 'use strict';

 const fs = require('fs');
 const path = require('path');
 
let getFileList = function(dirPath) {
    // Validate and sanitize directory path to prevent path traversal
    const baseDir = path.resolve(process.cwd()); // Use current working directory as base
    const sanitizedPath = dirPath.replace(/[^a-zA-Z0-9_.-/]/g, '');
    const absolutePath = path.join(baseDir, sanitizedPath);
    
    // Ensure the path doesn't escape the intended directory
    if (!path.normalize(absolutePath).startsWith(baseDir)) {
        throw new Error('Security violation: attempted directory traversal');
    }
    
    let fileInfo;
    let filesFound;
    let fileList = [];

    filesFound = fs.readdirSync(absolutePath);
    for (let i = 0; i < filesFound.length; i++) {
        // Sanitize file name
        const sanitizedFile = filesFound[i].replace(/[^a-zA-Z0-9_.-]/g, '');
        const fullPath = path.join(absolutePath, sanitizedFile);
        
        // Ensure the path doesn't escape the intended directory
        if (!path.normalize(fullPath).startsWith(baseDir)) {
            throw new Error('Security violation: attempted directory traversal');
        }
        
        fileInfo = fs.lstatSync(fullPath);
        fileInfo = fs.lstatSync(fullPath);
        if (fileInfo.isFile()) {
            fileList.push(sanitizedFile);
        }

        if (fileInfo.isDirectory()) {
            console.log(fullPath);
        }

    return fileList;
};
 
 // List all files in a directory in Node.js recursively in a synchronous fashion
let walkSync = function(dir, filelist) {
    // Validate and sanitize directory path to prevent path traversal
    const baseDir = path.resolve(process.cwd()); // Use current working directory as base
    const sanitizedDir = dir.replace(/[^a-zA-Z0-9_.-/]/g, '');
    const absolutePath = path.join(baseDir, sanitizedDir);
    
    // Ensure the path doesn't escape the intended directory
    if (!path.normalize(absolutePath).startsWith(baseDir)) {
        throw new Error('Security violation: attempted directory traversal');
    }
    
    let files = fs.readdirSync(absolutePath);
    filelist = filelist || [];
    files.forEach(function(file) {
        // Sanitize file name
        const sanitizedFile = file.replace(/[^a-zA-Z0-9_.-]/g, '');
        const fullPath = path.join(absolutePath, sanitizedFile);
        
        // Ensure the path doesn't escape the intended directory
        if (!path.normalize(fullPath).startsWith(baseDir)) {
            throw new Error('Security violation: attempted directory traversal');
        }
        
        if (fs.statSync(fullPath).isDirectory()) {
            // Properly construct the path for recursive call
            // Using the relative path from the base directory to maintain security checks
            const relativePath = path.relative(baseDir, fullPath);
            filelist = walkSync(relativePath, filelist);
        } else {
            filelist.push(fullPath);
        }
    });

    return filelist;
};
 
 let _filelist = [];
 let _manifest = {
     files: []
 };
const WEB_SITE_PATH = '../regional-s3-assets/web_site';
walkSync(WEB_SITE_PATH, _filelist);
 
 for (let i = 0; i < _filelist.length; i++) {
    _manifest.files.push(_filelist[i].replace(WEB_SITE_PATH + '/', ''));
};
 
 console.log(_manifest);
fs.writeFile('../regional-s3-assets/web-site-manifest.json', JSON.stringify(_manifest, null, 4), (err) => {
     if (err) throw err;
     console.log('The file has been saved!');
 });
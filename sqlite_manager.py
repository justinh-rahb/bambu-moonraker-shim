"""
SQLite database manager for file caching, metadata, and job history.
Handles structured data that benefits from queries and indexing.
UI configuration still uses database_manager.py (JSON-based).
"""

import sqlite3
import time
from typing import List, Dict, Optional, Any
from contextlib import contextmanager
import json


class SQLiteManager:
    def __init__(self, db_path: str = "bambu_shim.db"):
        self.db_path = db_path
        self._init_database()
    
    @contextmanager
    def get_connection(self):
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Enable dict-like access
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()
    
    def _init_database(self):
        """Initialize database schema."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # File cache table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS file_cache (
                    path TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    modified REAL NOT NULL,
                    is_dir INTEGER NOT NULL,
                    fetched_at REAL NOT NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_fetched_at ON file_cache(fetched_at)")
            
            # File metadata table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS file_metadata (
                    filename TEXT PRIMARY KEY,
                    slicer TEXT,
                    layer_height REAL,
                    first_layer_height REAL,
                    estimated_time INTEGER,
                    filament_total REAL,
                    thumbnails TEXT,
                    cached_at REAL NOT NULL
                )
            """)
            
            # Job history table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS job_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT UNIQUE,
                    filename TEXT NOT NULL,
                    start_time REAL NOT NULL,
                    end_time REAL,
                    total_duration REAL,
                    status TEXT,
                    filament_used REAL,
                    metadata TEXT
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_start_time ON job_history(start_time DESC)")
            
            conn.commit()
    
    # === File Cache Methods ===
    
    def cache_files(self, files: List[Dict[str, Any]], ttl: int = 300):
        """
        Cache file listing from FTPS.
        
        Args:
            files: List of file dicts with keys: name, size, modified, is_dir
            ttl: Time to live in seconds (default 5 minutes)
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            fetched_at = time.time()
            
            for file in files:
                cursor.execute("""
                    INSERT OR REPLACE INTO file_cache 
                    (path, filename, size, modified, is_dir, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    file.get('path', file['name']),
                    file['name'],
                    file['size'],
                    file['modified'],
                    1 if file['is_dir'] else 0,
                    fetched_at
                ))
    
    def get_cached_files(self, max_age: int = 300) -> Optional[List[Dict[str, Any]]]:
        """
        Get cached file listing if not stale.
        
        Args:
            max_age: Maximum age in seconds (default 5 minutes)
        
        Returns:
            List of file dicts or None if cache is stale/empty
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Check if cache exists and is fresh
            cursor.execute("SELECT MAX(fetched_at) as latest FROM file_cache")
            row = cursor.fetchone()
            
            if not row['latest']:
                return None
            
            age = time.time() - row['latest']
            if age > max_age:
                return None
            
            # Return cached files
            cursor.execute("""
                SELECT filename as name, size, modified, is_dir, path
                FROM file_cache
                ORDER BY is_dir DESC, filename ASC
            """)
            
            files = []
            for row in cursor.fetchall():
                files.append({
                    'name': row['name'],
                    'size': row['size'],
                    'modified': row['modified'],
                    'is_dir': bool(row['is_dir']),
                    'path': row['path']
                })
            
            return files
    
    def clear_file_cache(self):
        """Clear all cached files."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM file_cache")
    
    # === File Metadata Methods ===
    
    def cache_file_metadata(self, filename: str, metadata: Dict[str, Any]):
        """
        Cache metadata for a specific file.
        
        Args:
            filename: Name of the file
            metadata: Dict with keys: slicer, layer_height, estimated_time, thumbnails, etc.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO file_metadata
                (filename, slicer, layer_height, first_layer_height, estimated_time, 
                 filament_total, thumbnails, cached_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                filename,
                metadata.get('slicer'),
                metadata.get('layer_height'),
                metadata.get('first_layer_height'),
                metadata.get('estimated_time'),
                metadata.get('filament_total'),
                json.dumps(metadata.get('thumbnails', [])),
                time.time()
            ))
    
    def get_file_metadata(self, filename: str, max_age: int = 3600) -> Optional[Dict[str, Any]]:
        """
        Get cached metadata for a file.
        
        Args:
            filename: Name of the file
            max_age: Maximum age in seconds (default 1 hour)
        
        Returns:
            Metadata dict or None if not cached or stale
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM file_metadata WHERE filename = ?
            """, (filename,))
            
            row = cursor.fetchone()
            if not row:
                return None
            
            age = time.time() - row['cached_at']
            if age > max_age:
                return None
            
            return {
                'slicer': row['slicer'],
                'layer_height': row['layer_height'],
                'first_layer_height': row['first_layer_height'],
                'estimated_time': row['estimated_time'],
                'filament_total': row['filament_total'],
                'thumbnails': json.loads(row['thumbnails']) if row['thumbnails'] else []
            }
    
    # === Job History Methods ===
    
    def add_job(self, job_data: Dict[str, Any]) -> int:
        """
        Add a job to history.
        
        Args:
            job_data: Dict with keys: job_id, filename, start_time, end_time, 
                     total_duration, status, filament_used, metadata
        
        Returns:
            Row ID of inserted job
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO job_history
                (job_id, filename, start_time, end_time, total_duration, status, 
                 filament_used, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job_data.get('job_id'),
                job_data['filename'],
                job_data['start_time'],
                job_data.get('end_time'),
                job_data.get('total_duration'),
                job_data.get('status', 'unknown'),
                job_data.get('filament_used'),
                json.dumps(job_data.get('metadata', {}))
            ))
            return cursor.lastrowid
    
    def update_job(self, job_id: str, updates: Dict[str, Any]):
        """
        Update an existing job.
        
        Args:
            job_id: Unique job identifier
            updates: Dict of fields to update
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Build dynamic UPDATE query
            fields = []
            values = []
            for key, value in updates.items():
                if key in ['end_time', 'total_duration', 'status', 'filament_used', 'metadata']:
                    fields.append(f"{key} = ?")
                    if key == 'metadata':
                        values.append(json.dumps(value))
                    else:
                        values.append(value)
            
            if not fields:
                return
            
            values.append(job_id)
            query = f"UPDATE job_history SET {', '.join(fields)} WHERE job_id = ?"
            cursor.execute(query, values)
    
    def get_job_history(self, limit: int = 50, before: Optional[int] = None, 
                       since: Optional[int] = None, order: str = "desc") -> Dict[str, Any]:
        """
        Get job history with pagination.
        
        Args:
            limit: Maximum number of jobs to return
            before: Unix timestamp - return jobs before this time
            since: Unix timestamp - return jobs after this time
            order: "asc" or "desc" for chronological order
        
        Returns:
            Dict with 'count' and 'jobs' list
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Build query with filters
            where_clauses = []
            params = []
            
            if before:
                where_clauses.append("start_time < ?")
                params.append(before)
            
            if since:
                where_clauses.append("start_time > ?")
                params.append(since)
            
            where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
            order_sql = "DESC" if order.lower() == "desc" else "ASC"
            
            # Get total count
            cursor.execute(f"SELECT COUNT(*) as count FROM job_history {where_sql}", params)
            total_count = cursor.fetchone()['count']
            
            # Get jobs
            params.append(limit)
            cursor.execute(f"""
                SELECT * FROM job_history 
                {where_sql}
                ORDER BY start_time {order_sql}
                LIMIT ?
            """, params)
            
            jobs = []
            for row in cursor.fetchall():
                jobs.append({
                    'job_id': row['job_id'],
                    'filename': row['filename'],
                    'start_time': row['start_time'],
                    'end_time': row['end_time'],
                    'total_duration': row['total_duration'],
                    'status': row['status'],
                    'filament_used': row['filament_used'],
                    'metadata': json.loads(row['metadata']) if row['metadata'] else {}
                })
            
            return {
                'count': total_count,
                'jobs': jobs
            }
    
    def get_job_totals(self) -> Dict[str, Any]:
        """
        Get aggregate job statistics.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT 
                    COUNT(*) as total_jobs,
                    SUM(total_duration) as total_time,
                    SUM(filament_used) as total_filament,
                    MAX(total_duration) as longest_print
                FROM job_history
                WHERE status = 'completed'
            """)
            
            row = cursor.fetchone()
            
            return {
                "total_jobs": row['total_jobs'] or 0,
                "total_time": row['total_time'] or 0.0,
                "total_filament": row['total_filament'] or 0.0,
                "longest_job": row['longest_print'] or 0.0,
                "total_prints": row['total_jobs'] or 0  # Moonraker often duplicates this
            }

    def clear_old_jobs(self, days: int = 30):
        """
        Delete jobs older than specified days.
        
        Args:
            days: Number of days to keep
        """
        cutoff = time.time() - (days * 86400)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM job_history WHERE start_time < ?", (cutoff,))
            return cursor.rowcount


# Singleton instance
_sqlite_manager = None

def get_sqlite_manager() -> SQLiteManager:
    """Get singleton SQLite manager instance."""
    global _sqlite_manager
    if _sqlite_manager is None:
        _sqlite_manager = SQLiteManager()
    return _sqlite_manager

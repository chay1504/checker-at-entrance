import sqlite3
import pandas as pd

def view_database():
    try:
        # Connect to the SQLite database
        conn = sqlite3.connect('workshop.db')
        
        # Read the entire table using Pandas for a nice layout
        df = pd.read_sql_query("SELECT * FROM attendees", conn)
        
        if df.empty:
            print("The database is currently empty. No attendees have been imported yet.")
        else:
            print("\n--- Current Database Records ---")
            # Print all rows and columns
            print(df.to_string(index=False))
            print("--------------------------------\n")
            
        conn.close()
    except sqlite3.OperationalError:
        print("Database 'workshop.db' not found. It will be created when you run app.py.")
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == '__main__':
    view_database()

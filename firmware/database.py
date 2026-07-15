# database.py
import json
import math

class MaterialDatabase:
    def __init__(self, filename='database.json'):
        self.filename = filename
        self.data = self.load_db()

    def load_db(self):
        """Loads the JSON database from flash memory."""
        try:
            with open(self.filename, 'r') as f:
                return json.load(f)
        except (OSError, ValueError):
            # If the file doesn't exist or is empty/corrupt, return an empty dictionary
            return {}

    def save_db(self):
        """Saves the current dictionary to the JSON file."""
        with open(self.filename, 'w') as f:
            json.dump(self.data, f)

    def add_material(self, name, reflectance_data):
        """Adds a new spectral signature to the database and saves it."""
        self.data[name] = reflectance_data
        self.save_db()

    def calculate_similarity(self, measured_dict, db_dict):
        """Calculates Cosine Similarity between two 18-channel readings."""
        channels = ['A','B','C','D','E','F','G','H','I','J','K','L','R','S','T','U','V','W']
        
        dot_product = 0.0
        norm_a = 0.0
        norm_b = 0.0
        
        for ch in channels:
            # Default to 0 if a channel is somehow missing
            v1 = measured_dict.get(ch, 0.0) 
            v2 = db_dict.get(ch, 0.0)
            
            dot_product += (v1 * v2)
            norm_a += (v1 * v1)
            norm_b += (v2 * v2)
            
        if norm_a == 0 or norm_b == 0:
            return 0.0
            
        # Cosine similarity formula
        similarity = dot_product / (math.sqrt(norm_a) * math.sqrt(norm_b))
        
        # Constrain between 0 and 1, then convert to percentage
        return max(0.0, min(100.0, similarity * 100))

    def find_matches(self, measured_reflectance, top_n=3):
        """Compares a measurement against all items in the DB and returns top matches."""
        results = []
        for name, db_reflectance in self.data.items():
            sim = self.calculate_similarity(measured_reflectance, db_reflectance)
            results.append((name, sim))
        
        # Sort the list by highest percentage first
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:top_n]
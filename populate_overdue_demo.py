#!/usr/bin/env python
import os
import sys
import django
from django.utils import timezone
from datetime import timedelta

# Add the project directory to the Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# Set up Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'supply_.settings')
django.setup()

from inventory.models import User, Supply, BorrowedItem

def populate_overdue_demo_data():
    """
    Populate sample data for testing overdue item functionality
    """
    print("Populating overdue demo data...")
    
    # Get a department user (or create one if none exists)
    try:
        department_user = User.objects.filter(role='department_user').first()
        if not department_user:
            print("No department user found. Please create a department user first.")
            return
        print(f"Using department user: {department_user.username}")
    except Exception as e:
        print(f"Error getting department user: {e}")
        return
    
    # Get a supply item (or create one if none exists)
    try:
        supply = Supply.objects.first()
        if not supply:
            print("No supply items found. Please create some supply items first.")
            return
        print(f"Using supply item: {supply.name}")
    except Exception as e:
        print(f"Error getting supply item: {e}")
        return
    
    # Create an overdue borrowed item (due 2 days ago)
    try:
        overdue_item = BorrowedItem.objects.create(
            supply=supply,
            borrower=department_user,
            borrowed_quantity=1,
            location_when_borrowed="Main Office",
            notes="Demo overdue item for testing",
            return_deadline=timezone.now() - timedelta(days=2)  # 2 days overdue
        )
        print(f"Created overdue item: {overdue_item.supply.name} (ID: {overdue_item.id})")
        print(f"  - Borrowed by: {overdue_item.borrower.username}")
        print(f"  - Return deadline: {overdue_item.return_deadline}")
        print(f"  - Is overdue: {overdue_item.is_overdue}")
    except Exception as e:
        print(f"Error creating overdue item: {e}")
        return
    
    # Create a due soon borrowed item (due tomorrow)
    try:
        due_soon_item = BorrowedItem.objects.create(
            supply=supply,
            borrower=department_user,
            borrowed_quantity=1,
            location_when_borrowed="Storage Room",
            notes="Demo item due soon for testing",
            return_deadline=timezone.now() + timedelta(days=1)  # Due tomorrow
        )
        print(f"Created due soon item: {due_soon_item.supply.name} (ID: {due_soon_item.id})")
        print(f"  - Borrowed by: {due_soon_item.borrower.username}")
        print(f"  - Return deadline: {due_soon_item.return_deadline}")
        print(f"  - Days until due: {due_soon_item.days_until_due}")
    except Exception as e:
        print(f"Error creating due soon item: {e}")
        return
    
    # Create a returned item (should not be considered overdue)
    try:
        returned_item = BorrowedItem.objects.create(
            supply=supply,
            borrower=department_user,
            borrowed_quantity=1,
            location_when_borrowed="Reception",
            location_when_returned="Storage",
            returned_at=timezone.now() - timedelta(days=1),  # Returned 1 day ago
            notes="Demo returned item for testing",
            return_deadline=timezone.now() + timedelta(days=3)  # Was due in future
        )
        print(f"Created returned item: {returned_item.supply.name} (ID: {returned_item.id})")
        print(f"  - Borrowed by: {returned_item.borrower.username}")
        print(f"  - Returned at: {returned_item.returned_at}")
        print(f"  - Return deadline: {returned_item.return_deadline}")
        print(f"  - Is returned: {returned_item.is_returned}")
        print(f"  - Is overdue: {returned_item.is_overdue}")
    except Exception as e:
        print(f"Error creating returned item: {e}")
        return
    
    print("\nDemo data populated successfully!")
    print("You can now test the overdue item functionality:")
    print("1. Log in as the department user")
    print("2. Check the dashboard for overdue alerts")
    print("3. Try to borrow a new item (should be blocked due to overdue items)")
    print("4. Run the check_overdue_items management command to see alerts")

if __name__ == "__main__":
    populate_overdue_demo_data()
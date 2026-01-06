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

def fix_borrowed_items():
    """
    Fix the borrowed items data to properly test overdue functionality
    """
    print("Fixing borrowed items data...")
    print("=" * 50)
    
    # Get the department user
    try:
        dept_user = User.objects.get(username='dept')
        print(f"User: {dept_user.username}")
    except User.DoesNotExist:
        print("User 'dept' not found.")
        return
    
    # Get a supply item
    try:
        supply = Supply.objects.get(name='A4 Printer Paper')
        print(f"Supply: {supply.name}")
    except Supply.DoesNotExist:
        print("A4 Printer Paper not found.")
        return
    
    # Delete the problematic items from our previous test
    print("\nDeleting problematic test items...")
    BorrowedItem.objects.filter(id__in=[7, 8, 9]).delete()
    print("Deleted items with IDs 7, 8, 9")
    
    # Create a properly overdue item (borrowed 5 days ago, due 2 days ago)
    print("\nCreating properly overdue item...")
    overdue_item = BorrowedItem.objects.create(
        supply=supply,
        borrower=dept_user,
        borrowed_quantity=1,
        location_when_borrowed="Main Office",
        notes="Properly overdue item for testing",
        borrowed_at=timezone.now() - timedelta(days=5),
        return_deadline=timezone.now() - timedelta(days=2)  # 2 days overdue
    )
    print(f"Created overdue item: {overdue_item.supply.name} (ID: {overdue_item.id})")
    print(f"  - Borrowed: {overdue_item.borrowed_at}")
    print(f"  - Due: {overdue_item.return_deadline}")
    print(f"  - Overdue by: {(timezone.now() - overdue_item.return_deadline).days} days")
    print(f"  - Is overdue: {overdue_item.is_overdue}")
    
    # Create a due soon item (borrowed today, due in 1 day)
    print("\nCreating due soon item...")
    due_soon_item = BorrowedItem.objects.create(
        supply=supply,
        borrower=dept_user,
        borrowed_quantity=1,
        location_when_borrowed="Storage Room",
        notes="Due soon item for testing",
        borrowed_at=timezone.now(),
        return_deadline=timezone.now() + timedelta(days=1)  # Due tomorrow
    )
    print(f"Created due soon item: {due_soon_item.supply.name} (ID: {due_soon_item.id})")
    print(f"  - Borrowed: {due_soon_item.borrowed_at}")
    print(f"  - Due: {due_soon_item.return_deadline}")
    print(f"  - Days until due: {due_soon_item.days_until_due}")
    print(f"  - Is overdue: {due_soon_item.is_overdue}")
    
    # Create a properly returned item
    print("\nCreating properly returned item...")
    returned_item = BorrowedItem.objects.create(
        supply=supply,
        borrower=dept_user,
        borrowed_quantity=1,
        location_when_borrowed="Reception",
        location_when_returned="Storage",
        borrowed_at=timezone.now() - timedelta(days=3),
        returned_at=timezone.now() - timedelta(days=1),  # Returned 1 day ago
        notes="Properly returned item for testing"
        # No return_deadline needed for returned items
    )
    print(f"Created returned item: {returned_item.supply.name} (ID: {returned_item.id})")
    print(f"  - Borrowed: {returned_item.borrowed_at}")
    print(f"  - Returned: {returned_item.returned_at}")
    print(f"  - Duration: {returned_item.duration_display}")
    print(f"  - Is returned: {returned_item.is_returned}")
    print(f"  - Is overdue: {returned_item.is_overdue}")
    
    print("\nFixed borrowed items data successfully!")
    print("You should now see the overdue item restriction working correctly.")

if __name__ == "__main__":
    fix_borrowed_items()